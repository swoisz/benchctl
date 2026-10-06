"""Error hierarchy. The wire code alone decides the exit status (design Section 7.1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_EXIT_CODES = {
    "timeout": 1,
    "assert": 1,
    "usage": 2,
    "busy": 3,
    "device": 4,
    "internal": 4,
    "slow_client": 4,
    "protocol": 4,
}


def exit_code(code: str) -> int:
    """Process exit status for an error code; unknown codes are daemon errors (4)."""
    return _EXIT_CODES.get(code, 4)


class BenchctlError(Exception):
    """Base error. `code` is the wire code; `data` rides along in error JSON."""

    code: str = "internal"

    def __init__(
        self, message: str, *, code: str | None = None, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        # Wire-only codes (slow_client, protocol) have no subclass, so the base takes one.
        if code is not None:
            self.code = code
        self.message = message
        self.data: dict[str, Any] = {} if data is None else data

    @property
    def exit_code(self) -> int:
        return exit_code(self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "data": self.data}


class UsageError(BenchctlError):
    code = "usage"


class BusyError(BenchctlError):
    code = "busy"


class DeviceError(BenchctlError):
    code = "device"


class TimeoutError_(BenchctlError):  # trailing underscore: the builtin TimeoutError stays usable
    code = "timeout"


class AssertionFailed(BenchctlError):
    code = "assert"


class InternalError(BenchctlError):
    code = "internal"


class TransportLost(DeviceError):
    """The port vanished under a read or write; the reader reconnects, callers see `device`."""


_BY_CODE: dict[str, type[BenchctlError]] = {
    cls.code: cls
    for cls in (UsageError, BusyError, DeviceError, TimeoutError_, AssertionFailed, InternalError)
}


def from_dict(error: Mapping[str, Any]) -> BenchctlError:
    """Rebuild an error from its wire form (`{"code", "message", "data"}`)."""
    code = str(error.get("code", "internal"))
    message = str(error.get("message", ""))
    data = dict(error.get("data") or {})
    cls = _BY_CODE.get(code)
    if cls is None:
        return BenchctlError(message, code=code, data=data)
    return cls(message, data=data)
