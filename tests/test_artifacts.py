"""Flash input resolution, sha256 and app-desc parsing (design Section 4.10)."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from benchctl import errors
from benchctl.artifacts import (
    APP_DESC_MAGIC,
    APP_DESC_OFFSET,
    FLASHER_ARGS,
    read_app_desc,
    resolve,
    sha256_file,
)
from benchctl.types import AppDesc

KEEP_ALL = {"flash-mode": "keep", "flash-size": "keep", "flash-freq": "keep"}
ELF_SHA = bytes(range(32))
IMAGE_HEADER = b"\xe9" + bytes(23)  # magic byte, then the rest of the 24-byte header
SEGMENT_HEADER = bytes(8)


def app_image(magic: int = APP_DESC_MAGIC) -> bytes:
    desc = struct.pack(
        "<II8s32s32s16s16s32s32s",
        magic,
        0,
        bytes(8),
        b"1.2.3-4-gabc",
        b"fakeapp",
        b"12:42:21",
        b"Oct  6 2026",
        b"v5.4",
        ELF_SHA,
    )
    return IMAGE_HEADER + SEGMENT_HEADER + desc + bytes(64)


def flasher_args(**overrides: Any) -> dict[str, Any]:
    """The shape idf.py writes: underscore flags and a superset of named entries."""
    data: dict[str, Any] = {
        "write_flash_args": ["--flash_mode", "dio", "--flash_size", "keep", "--flash_freq", "80m"],
        "flash_settings": {"flash_mode": "dio", "flash_size": "keep", "flash_freq": "80m"},
        "flash_files": {
            "0x0": "bootloader/bootloader.bin",
            "0x20000": "app.bin",
            "0x10000": "partition_table/partition-table.bin",
            "0x1c000": "ota_data_initial.bin",
            "0x400000": "storage.bin",
        },
        "bootloader": {"offset": "0x0", "file": "bootloader/bootloader.bin", "encrypted": "false"},
        "app": {"offset": "0x20000", "file": "app.bin", "encrypted": "false"},
        "partition-table": {
            "offset": "0x10000",
            "file": "partition_table/partition-table.bin",
            "encrypted": "false",
        },
        "otadata": {"offset": "0x1c000", "file": "ota_data_initial.bin", "encrypted": "false"},
        "extra_esptool_args": {"after": "hard_reset", "before": "default_reset", "chip": "esp32s3"},
    }
    data.update(overrides)
    return data


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    for relative in flasher_args()["flash_files"].values():
        path = build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(app_image() if relative == "app.bin" else relative.encode())
    (build / FLASHER_ARGS).write_text(json.dumps(flasher_args()), encoding="utf-8")
    return build


def expected_files(build: Path) -> list[tuple[int, str, str]]:
    root = build.resolve()
    return [
        (0x0, str(root / "bootloader/bootloader.bin"), "bootloader"),
        (0x10000, str(root / "partition_table/partition-table.bin"), "partition-table"),
        (0x1C000, str(root / "ota_data_initial.bin"), "otadata"),
        (0x20000, str(root / "app.bin"), "app"),
        (0x400000, str(root / "storage.bin"), "0x400000"),
    ]


# --- resolve -----------------------------------------------------------------------------


def test_build_dir_resolves_every_flash_file_with_hyphen_flags(build_dir: Path) -> None:
    files, flash_args = resolve([str(build_dir)], None, {})
    assert files == expected_files(build_dir)
    assert flash_args == {"flash-mode": "dio", "flash-size": "keep", "flash-freq": "80m"}


def test_flasher_args_json_resolves_relative_to_its_dir(
    build_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    files, flash_args = resolve([f"build/{FLASHER_ARGS}"], None, {})
    assert files == expected_files(build_dir)
    assert flash_args["flash-mode"] == "dio"


def test_addr_file_pairs_are_named_by_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.bin").write_bytes(b"app")
    (tmp_path / "ota.bin").write_bytes(b"ota")
    monkeypatch.chdir(tmp_path)

    files, flash_args = resolve(["0x220000=app.bin", "53248=ota.bin"], None, {})

    root = tmp_path.resolve()
    assert files == [
        (0xD000, str(root / "ota.bin"), "0xd000"),
        (0x220000, str(root / "app.bin"), "0x220000"),
    ]
    assert flash_args == KEEP_ALL


def test_only_filters_by_name(build_dir: Path) -> None:
    files, _ = resolve([str(build_dir)], ["app", "otadata"], {})
    assert [name for _, _, name in files] == ["otadata", "app"]

    with pytest.raises(errors.UsageError, match="--only nvs: not among bootloader"):
        resolve([str(build_dir)], ["nvs"], {})


def test_overrides_beat_the_build_and_accept_either_spelling(build_dir: Path) -> None:
    _, flash_args = resolve([str(build_dir)], None, {"flash_size": "8MB", "flash-freq": "40m"})
    assert flash_args == {"flash-mode": "dio", "flash-size": "8MB", "flash-freq": "40m"}
    with pytest.raises(errors.UsageError, match="unknown flash setting 'baud'"):
        resolve([str(build_dir)], None, {"baud": "921600"})


@pytest.mark.parametrize(
    ("write_flash_args", "expected"),
    [
        (
            ["--flash-mode", "qio", "--flash-size", "4MB"],
            {"flash-mode": "qio", "flash-size": "4MB"},
        ),
        (
            ["--flash_mode=dout", "--compress", "--flash_freq", "40m"],
            {"flash-mode": "dout", "flash-freq": "40m"},
        ),
        (None, {"flash-mode": "dio", "flash-freq": "80m"}),
    ],
)
def test_flash_settings_parse_from_either_field(
    build_dir: Path, write_flash_args: list[str] | None, expected: dict[str, str]
) -> None:
    data = flasher_args(write_flash_args=write_flash_args)
    if write_flash_args is None:
        del data["write_flash_args"]
    (build_dir / FLASHER_ARGS).write_text(json.dumps(data), encoding="utf-8")
    _, flash_args = resolve([str(build_dir)], None, {})
    assert flash_args == {**KEEP_ALL, **expected}


def test_bad_inputs_are_usage_errors(build_dir: Path, tmp_path: Path) -> None:
    (tmp_path / "loose.bin").write_bytes(b"x")
    (tmp_path / "empty").mkdir()
    cases = {
        "nothing to flash": [],
        "no such file": [str(tmp_path / "missing")],
        "not an ESP-IDF build dir": [str(tmp_path / "empty")],
        "expected a build dir": [str(tmp_path / "loose.bin")],
        "given twice": [str(build_dir), f"0x20000={tmp_path / 'loose.bin'}"],
        "does not exist": [f"0x1000={tmp_path / 'absent.bin'}"],
    }
    for message, specs in cases.items():
        with pytest.raises(errors.UsageError, match=message):
            resolve(specs, None, {})


def test_conflicting_build_settings_are_rejected(build_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "extra.bin").write_bytes(b"x")
    data = flasher_args(
        write_flash_args=["--flash_mode", "qio"], flash_files={"0x500000": "extra.bin"}
    )
    (other / FLASHER_ARGS).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(errors.UsageError, match="flash-mode qio disagrees"):
        resolve([str(build_dir), str(other)], None, {})


def test_flasher_args_without_flash_files_is_rejected(build_dir: Path) -> None:
    (build_dir / FLASHER_ARGS).write_text(json.dumps({"app": {}}), encoding="utf-8")
    with pytest.raises(errors.UsageError, match="no flash_files"):
        resolve([str(build_dir)], None, {})


# --- sha256 and app desc -----------------------------------------------------------------


def test_sha256_file_hashes_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    data = bytes(range(256)) * 9000  # spans more than one read chunk
    path.write_bytes(data)
    assert sha256_file(str(path)) == (hashlib.sha256(data).hexdigest(), len(data))
    with pytest.raises(errors.UsageError, match="cannot read"):
        sha256_file(str(tmp_path / "missing.bin"))


def test_read_app_desc_parses_a_synthetic_image(tmp_path: Path) -> None:
    path = tmp_path / "app.bin"
    path.write_bytes(app_image())
    assert read_app_desc(str(path)) == AppDesc(
        version="1.2.3-4-gabc",
        project="fakeapp",
        elf_sha256=ELF_SHA.hex(),
        idf_ver="v5.4",
        time="12:42:21",
        date="Oct  6 2026",
    )


def test_read_app_desc_rejects_non_app_images(tmp_path: Path) -> None:
    wrong_magic = tmp_path / "bootloader.bin"
    wrong_magic.write_bytes(app_image(magic=0x12345678))
    short = tmp_path / "short.bin"
    short.write_bytes(app_image()[: APP_DESC_OFFSET + 16])
    assert read_app_desc(str(wrong_magic)) is None
    assert read_app_desc(str(short)) is None
    with pytest.raises(errors.UsageError, match="cannot read"):
        read_app_desc(str(tmp_path / "missing.bin"))


def test_build_dir_app_entry_carries_an_app_desc(build_dir: Path) -> None:
    files, _ = resolve([str(build_dir)], ["app"], {})
    [(_, path, name)] = files
    assert name == "app"
    desc = read_app_desc(path)
    assert desc is not None
    assert desc.elf_sha256 == ELF_SHA.hex()
