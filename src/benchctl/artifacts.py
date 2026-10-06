"""Flash inputs: build dir, `flasher_args.json`, `addr=file` pairs; app desc; sha256
(design Sections 4.10 and 7.1 `flash`)."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any

from benchctl.errors import UsageError
from benchctl.types import AppDesc

FLASHER_ARGS = "flasher_args.json"
# flasher_args.json's named entries, which are also the `--only` vocabulary.
IMAGE_NAMES = ("app", "bootloader", "partition-table", "otadata")
FLASH_ARG_KEYS = ("flash-mode", "flash-size", "flash-freq")
KEEP = "keep"  # esptool's "leave the image header as built"

# esp_app_desc_t sits after the 24-byte image header and the first 8-byte segment header.
APP_DESC_OFFSET = 32
APP_DESC_MAGIC = 0xABCD5432
# magic, secure_version, reserv1[2], version, project_name, time, date, idf_ver, app_elf_sha256
_APP_DESC = struct.Struct("<II8s32s32s16s16s32s32s")
_HASH_CHUNK = 1 << 20
_PAIR = re.compile(r"(0[xX][0-9a-fA-F]+|[0-9]+)=(.+)")


def _flag_name(token: str) -> str:
    """`--flash_mode` and `--flash-mode` both name esptool 5's `flash-mode`."""
    return token.removeprefix("--").replace("_", "-")


def _flash_args_from_json(data: dict[str, Any], source: Path) -> dict[str, str]:
    args: dict[str, str] = {}
    tokens = data.get("write_flash_args")
    if isinstance(tokens, list):
        words = [str(token) for token in tokens]
        for index, word in enumerate(words):
            name, eq, inline = word.partition("=")
            key = _flag_name(name)
            if key not in FLASH_ARG_KEYS:
                continue
            if eq:
                args[key] = inline
            elif index + 1 < len(words):
                args[key] = words[index + 1]
            else:
                raise UsageError(f"{source}: write_flash_args: {word} has no value")
        return args
    settings = data.get("flash_settings")
    if isinstance(settings, dict):
        for name, value in settings.items():
            key = _flag_name(str(name))
            if key in FLASH_ARG_KEYS:
                args[key] = str(value)
    return args


def _parse_addr(text: str, source: Path | str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise UsageError(f"{source}: invalid flash address {text!r}") from None


def _from_flasher_args(path: Path) -> tuple[list[tuple[int, Path, str]], dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"{path}: {exc}") from exc
    flash_files = data.get("flash_files") if isinstance(data, dict) else None
    if not isinstance(flash_files, dict) or not flash_files:
        raise UsageError(f"{path}: no flash_files; is this an ESP-IDF flasher_args.json?")
    # flash_files is the complete set; the named entries only label some of its addresses.
    names: dict[int, str] = {}
    for name in IMAGE_NAMES:
        entry = data.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("offset"), str):
            names[_parse_addr(entry["offset"], path)] = name
    files: list[tuple[int, Path, str]] = []
    for addr_text, relative in flash_files.items():
        addr = _parse_addr(addr_text, path)
        files.append((addr, path.parent / str(relative), names.get(addr, hex(addr))))
    return files, _flash_args_from_json(data, path)


def _from_spec(spec: str) -> tuple[list[tuple[int, Path, str]], dict[str, str] | None]:
    pair = _PAIR.fullmatch(spec)
    if pair is not None:
        addr = _parse_addr(pair.group(1), spec)
        return [(addr, Path(pair.group(2)).expanduser(), hex(addr))], None
    path = Path(spec).expanduser()
    if path.is_dir():
        path = path / FLASHER_ARGS
        if not path.is_file():
            raise UsageError(f"{spec}: no {FLASHER_ARGS}; not an ESP-IDF build dir")
    elif not path.is_file():
        raise UsageError(f"{spec}: no such file or directory")
    elif path.suffix != ".json":
        raise UsageError(f"{spec}: expected a build dir, {FLASHER_ARGS}, or addr=file")
    return _from_flasher_args(path)


def resolve(
    specs: list[str], only: list[str] | None, overrides: dict[str, str]
) -> tuple[list[tuple[int, str, str]], dict[str, str]]:
    """`(addr, absolute path, name)` sorted by address, and the three esptool flash args.

    Names are flasher_args.json's (`app`, `bootloader`, `partition-table`, `otadata`), else
    the address in hex. `overrides` (keys `flash-mode`/`flash-size`/`flash-freq`, or their
    underscore forms) beat the build's settings; an unset one is `keep`. Relative paths
    resolve against the cwd, flasher_args.json entries against the json's directory.
    Raises UsageError.
    """
    if not specs:
        raise UsageError("nothing to flash: give a build dir, flasher_args.json, or addr=file")
    files: dict[int, tuple[Path, str, str]] = {}  # addr -> (path, name, spec)
    build_args: dict[str, tuple[str, str]] = {}  # key -> (value, spec)
    for spec in specs:
        entries, args = _from_spec(spec)
        for addr, path, name in entries:
            if addr in files:
                raise UsageError(f"address {addr:#x} given twice: {files[addr][2]} and {spec}")
            files[addr] = (path, name, spec)
        for key, value in (args or {}).items():
            prior = build_args.setdefault(key, (value, spec))
            if prior[0] != value:
                raise UsageError(f"{spec}: {key} {value} disagrees with {prior[1]} ({prior[0]})")

    flash_args = {key: build_args[key][0] if key in build_args else KEEP for key in FLASH_ARG_KEYS}
    for name, value in overrides.items():
        key = _flag_name(name)
        if key not in FLASH_ARG_KEYS:
            raise UsageError(f"unknown flash setting {name!r}; one of {', '.join(FLASH_ARG_KEYS)}")
        flash_args[key] = value

    selected = sorted(files.items())
    if only is not None:
        present = [name for _, (_, name, _) in selected]
        for name in only:
            if name not in present:
                raise UsageError(f"--only {name}: not among {', '.join(present)}")
        selected = [item for item in selected if item[1][1] in only]

    resolved: list[tuple[int, str, str]] = []
    for addr, (path, name, spec) in selected:
        if not path.is_file():
            raise UsageError(f"{spec}: {name} image {path} does not exist")
        resolved.append((addr, str(path.resolve()), name))
    return resolved, flash_args


def sha256_file(path: str) -> tuple[str, int]:
    """(hex sha256, size in bytes). Raises UsageError when the file cannot be read."""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as file:
            while chunk := file.read(_HASH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest(), size


def _c_string(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def read_app_desc(path: str) -> AppDesc | None:
    """The app description embedded in an app image (signed images keep it), or None when
    the file is too short or carries no app-desc magic. Raises UsageError if unreadable."""
    try:
        with open(path, "rb") as file:
            head = file.read(APP_DESC_OFFSET + _APP_DESC.size)
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if len(head) < APP_DESC_OFFSET + _APP_DESC.size:
        return None
    magic, _, _, version, project, time, date, idf_ver, elf_sha = _APP_DESC.unpack_from(
        head, APP_DESC_OFFSET
    )
    if magic != APP_DESC_MAGIC:
        return None
    return AppDesc(
        version=_c_string(version),
        project=_c_string(project),
        elf_sha256=elf_sha.hex(),
        idf_ver=_c_string(idf_ver),
        time=_c_string(time),
        date=_c_string(date),
    )
