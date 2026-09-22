"""Windows-only private discovery and single-instance runtime control adapter."""

from __future__ import annotations

import csv
import json
import msvcrt
import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self


class RuntimeControlError(RuntimeError):
    """Fail-closed runtime control or ACL failure."""


class RuntimeAlreadyActiveError(RuntimeControlError):
    """A cooperating service already owns the per-user runtime lock."""


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeDiscoveryRecord:
    instance_id: str
    pid: int
    port: int
    started_at: datetime
    launcher_capability: str
    protocol_version: str = "1"

    def __post_init__(self) -> None:
        if not self.instance_id or self.pid <= 0:
            raise ValueError("runtime discovery identity is invalid")
        if not 0 < self.port <= 65_535:
            raise ValueError("runtime discovery port is invalid")
        if len(self.launcher_capability) < 32:
            raise ValueError("launcher capability is too short")
        if self.started_at.tzinfo is None:
            raise ValueError("runtime discovery timestamp must be timezone-aware")

    @property
    def exact_host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def __repr__(self) -> str:
        return (
            "RuntimeDiscoveryRecord("
            f"instance_id={self.instance_id!r}, pid={self.pid}, port={self.port}, "
            f"started_at={self.started_at!r}, launcher_capability='<redacted>', "
            f"protocol_version={self.protocol_version!r})"
        )

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "instance_id": self.instance_id,
                    "pid": self.pid,
                    "port": self.port,
                    "started_at": self.started_at.astimezone(UTC).isoformat(),
                    "launcher_capability": self.launcher_capability,
                    "protocol_version": self.protocol_version,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> Self:
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != {
                "instance_id",
                "pid",
                "port",
                "started_at",
                "launcher_capability",
                "protocol_version",
            }:
                raise ValueError("unexpected discovery fields")
            return cls(
                instance_id=str(value["instance_id"]),
                pid=int(value["pid"]),
                port=int(value["port"]),
                started_at=datetime.fromisoformat(str(value["started_at"])),
                launcher_capability=str(value["launcher_capability"]),
                protocol_version=str(value["protocol_version"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeControlError("runtime discovery record is invalid") from error


class LoopbackSocketReservation:
    """Reserve an OS-selected loopback endpoint without a bind race."""

    def __init__(self) -> None:
        reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            reserved.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reserved.bind(("127.0.0.1", 0))
            reserved.listen(socket.SOMAXCONN)
        except BaseException:
            reserved.close()
            raise
        self._socket = reserved

    @property
    def socket(self) -> socket.socket:
        return self._socket

    @property
    def port(self) -> int:
        host, port = self._socket.getsockname()
        if host != "127.0.0.1":
            raise RuntimeControlError("reserved endpoint is not exact IPv4 loopback")
        return int(port)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class WindowsRuntimeControl:
    """Own a private runtime directory, lock, and sensitive discovery record."""

    def __init__(self, runtime_directory: Path) -> None:
        if os.name != "nt":
            raise RuntimeControlError("Windows runtime control requires Windows")
        self._directory = runtime_directory.resolve()
        self._lock_path = self._directory / "service.lock"
        self._discovery_path = self._directory / "discovery.json"
        self._lock_stream: BinaryIO | None = None
        self._current_user_sid = _current_windows_user_sid()

    @property
    def discovery_path(self) -> Path:
        return self._discovery_path

    def acquire(self) -> None:
        if self._lock_stream is not None:
            raise RuntimeControlError("runtime control is already acquired")
        self._directory.mkdir(parents=True, exist_ok=True)
        _restrict_acl(self._directory, self._current_user_sid, container=True)
        stream = self._lock_path.open("a+b")
        try:
            _restrict_acl(self._lock_path, self._current_user_sid, container=False)
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            stream.close()
            raise RuntimeAlreadyActiveError("runtime lock is already held") from error
        except BaseException:
            stream.close()
            raise
        self._lock_stream = stream

    def publish(self, record: RuntimeDiscoveryRecord) -> None:
        if self._lock_stream is None:
            raise RuntimeControlError("runtime lock must be acquired before discovery publish")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="discovery-",
                suffix=".tmp",
                dir=self._directory,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(record.to_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            _restrict_acl(temporary_path, self._current_user_sid, container=False)
            os.replace(temporary_path, self._discovery_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def read(self) -> RuntimeDiscoveryRecord:
        try:
            return RuntimeDiscoveryRecord.from_json_bytes(self._discovery_path.read_bytes())
        except OSError as error:
            raise RuntimeControlError("runtime discovery record is unavailable") from error

    def lock_is_held(self) -> bool:
        """Observe the cooperating service lock without trusting PID or discovery alone."""
        if not self._lock_path.is_file():
            return False
        stream = self._lock_path.open("r+b")
        try:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        finally:
            stream.close()

    def close(self) -> None:
        if self._lock_stream is None:
            return
        self._discovery_path.unlink(missing_ok=True)
        self._lock_stream.seek(0)
        msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_UNLCK, 1)
        self._lock_stream.close()
        self._lock_stream = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def _system_tool(name: str) -> str:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    candidate = root / "System32" / name
    if not candidate.is_file():
        raise RuntimeControlError(f"required Windows system tool is unavailable: {name}")
    return str(candidate)


def _current_windows_user_sid() -> str:
    completed = subprocess.run(
        [_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = tuple(csv.reader(completed.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeControlError("could not resolve the current Windows user SID")
    return rows[0][1]


def _restrict_acl(path: Path, user_sid: str, *, container: bool) -> None:
    inheritance = "(OI)(CI)F" if container else "F"
    completed = subprocess.run(
        [
            _system_tool("icacls.exe"),
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{user_sid}:{inheritance}",
            "/grant:r",
            f"*S-1-5-18:{inheritance}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeControlError("failed to enforce private runtime ACL")
