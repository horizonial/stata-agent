"""Application-private JSONL diagnostics sink with bounded retention."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stata_research_agent.application.diagnostics import (
    DiagnosticEvent,
    DiagnosticEventCandidate,
    DiagnosticEventDefinition,
    DiagnosticHealth,
    utc_now,
)


class FilesystemDiagnosticSink:
    """Store only validated event envelopes outside every Workspace."""

    def __init__(
        self,
        root: Path,
        *,
        rotate_bytes: int = 8 * 1024 * 1024,
        max_age_days: int = 14,
        max_total_bytes: int = 256 * 1024 * 1024,
        crash_max_age_days: int = 30,
        crash_max_count: int = 20,
    ) -> None:
        if (
            min(
                rotate_bytes,
                max_age_days,
                max_total_bytes,
                crash_max_age_days,
                crash_max_count,
            )
            < 1
        ):
            raise ValueError("Diagnostic retention limits must be positive")
        self._root = root.resolve()
        self._logs = self._root / "logs"
        self._crashes = self._root / "crash-capsules"
        self._health_path = self._root / "diagnostics_health.json"
        self._state_path = self._root / "diagnostics_state.json"
        self._rotate_bytes = rotate_bytes
        self._max_age = timedelta(days=max_age_days)
        self._max_total_bytes = max_total_bytes
        self._crash_max_age = timedelta(days=crash_max_age_days)
        self._crash_max_count = crash_max_count
        self._lock = threading.Lock()
        self._logs.mkdir(parents=True, exist_ok=True)
        self._crashes.mkdir(parents=True, exist_ok=True)
        self._sequence, self._generation = self._load_state()
        if not self._health_path.exists():
            self._write_health(DiagnosticHealth(None, None, None, self._generation))

    def append(
        self,
        candidate: DiagnosticEventCandidate,
        definition: DiagnosticEventDefinition,
        *,
        release_id: str,
        build_id: str,
        instance_id: str,
    ) -> DiagnosticEvent:
        with self._lock:
            now = utc_now()
            try:
                active = self._active_log()
                if active.exists() and active.stat().st_size >= self._rotate_bytes:
                    self._generation += 1
                    active = self._active_log()
                self._sequence += 1
                event = DiagnosticEvent(
                    f"diag_{self._sequence:020d}",
                    self._sequence,
                    "1.0",
                    definition.event_schema_version,
                    now,
                    now,
                    release_id,
                    build_id,
                    instance_id,
                    candidate.event_name,
                    candidate.severity_number,
                    candidate.severity_text,
                    candidate.safe_code,
                    candidate.component,
                    candidate.process_role,
                    dict(candidate.safe_attributes),
                    candidate.trace_id,
                    candidate.span_id,
                    candidate.workspace_ref,
                    candidate.turn_id,
                    candidate.operation_id,
                    candidate.attempt_id,
                    candidate.duration_ms,
                    candidate.parent_span_id,
                )
                line = json.dumps(
                    event.to_payload(),
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                with active.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._write_state()
                self._write_health(DiagnosticHealth(now, None, None, self._generation))
                return event
            except Exception as error:
                self._sequence, self._generation = self._load_state()
                previous = self.health()
                degraded = previous.degraded_since or now
                try:
                    self._write_health(
                        DiagnosticHealth(
                            previous.last_successful_write_at,
                            f"DIAGNOSTIC_SINK_{type(error).__name__.upper()}",
                            degraded,
                            self._generation,
                        )
                    )
                except OSError:
                    pass
                raise RuntimeError("diagnostics_degraded") from None

    def snapshot_end(self) -> int:
        with self._lock:
            return self._sequence

    def read_through(self, sequence: int) -> tuple[DiagnosticEvent, ...]:
        events: list[DiagnosticEvent] = []
        for path in sorted(self._logs.glob("diagnostics-*.jsonl")):
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    payload = json.loads(line)
                    event_sequence = int(payload["sequence"])
                    if event_sequence <= sequence:
                        events.append(self._event_from_payload(payload))
        return tuple(sorted(events, key=lambda item: item.sequence))

    def health(self) -> DiagnosticHealth:
        try:
            payload = json.loads(self._health_path.read_text(encoding="utf-8"))
            return DiagnosticHealth(
                payload.get("last_successful_write_at"),
                payload.get("last_failure_code"),
                payload.get("degraded_since"),
                int(payload.get("sink_generation", self._generation)),
            )
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return DiagnosticHealth(None, "DIAGNOSTIC_HEALTH_UNAVAILABLE", None, 0)

    def enforce_retention(self) -> Mapping[str, int]:
        now = datetime.now(UTC)
        deleted_logs = 0
        log_files = sorted(
            self._logs.glob("diagnostics-*.jsonl"), key=lambda item: item.stat().st_mtime
        )
        active = self._active_log()
        for path in list(log_files):
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if path != active and now - modified > self._max_age:
                path.unlink(missing_ok=True)
                deleted_logs += 1
                log_files.remove(path)
        total = sum(path.stat().st_size for path in log_files if path.exists())
        for path in list(log_files):
            if total <= self._max_total_bytes or path == active:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            deleted_logs += 1

        capsules = sorted(self._crashes.glob("crash-*.json"), key=lambda item: item.stat().st_mtime)
        deleted_capsules = 0
        survivors: list[Path] = []
        for path in capsules:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if now - modified > self._crash_max_age:
                path.unlink(missing_ok=True)
                deleted_capsules += 1
            else:
                survivors.append(path)
        while len(survivors) > self._crash_max_count:
            survivors.pop(0).unlink(missing_ok=True)
            deleted_capsules += 1
        return {"logs_deleted": deleted_logs, "capsules_deleted": deleted_capsules}

    def clear(self) -> Mapping[str, int]:
        deleted_logs = 0
        deleted_capsules = 0
        with self._lock:
            for path in self._logs.glob("diagnostics-*.jsonl"):
                path.unlink(missing_ok=True)
                deleted_logs += 1
            for path in self._crashes.glob("crash-*.json"):
                path.unlink(missing_ok=True)
                deleted_capsules += 1
            self._sequence = 0
            self._generation += 1
            self._write_state()
            self._write_health(DiagnosticHealth(None, None, None, self._generation))
        return {"logs_deleted": deleted_logs, "capsules_deleted": deleted_capsules}

    def write_crash_capsule(self, *, capsule_id: str, payload: Mapping[str, Any]) -> Path:
        if not capsule_id or any(character not in "0123456789abcdef" for character in capsule_id):
            raise ValueError("Crash Capsule identity must be lowercase hexadecimal")
        target = self._crashes / f"crash-{capsule_id}.json"
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(target, encoded)
        return target

    def read_crash_capsules(self) -> tuple[Mapping[str, Any], ...]:
        capsules: list[Mapping[str, Any]] = []
        for path in sorted(self._crashes.glob("crash-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                capsules.append(payload)
        return tuple(capsules)

    def _active_log(self) -> Path:
        return self._logs / f"diagnostics-{self._generation:06d}.jsonl"

    def _load_state(self) -> tuple[int, int]:
        sequence = 0
        generation = 1
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            sequence = int(payload["last_sequence"])
            generation = int(payload["sink_generation"])
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
        for path in self._logs.glob("diagnostics-*.jsonl"):
            try:
                generation = max(generation, int(path.stem.split("-")[-1]))
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        sequence = max(sequence, int(json.loads(line)["sequence"]))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return sequence, generation

    def _write_state(self) -> None:
        self._atomic_write(
            self._state_path,
            json.dumps(
                {
                    "last_sequence": self._sequence,
                    "sink_generation": self._generation,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    def _write_health(self, health: DiagnosticHealth) -> None:
        self._atomic_write(
            self._health_path,
            json.dumps(
                {
                    "last_successful_write_at": health.last_successful_write_at,
                    "last_failure_code": health.last_failure_code,
                    "degraded_since": health.degraded_since,
                    "sink_generation": health.sink_generation,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _event_from_payload(payload: Mapping[str, Any]) -> DiagnosticEvent:
        if payload.get("diagnostic_schema_version") != "1.0":
            sequence = int(payload.get("sequence", 0))
            return DiagnosticEvent(
                f"diag_unknown_{sequence:020d}",
                sequence,
                "unknown",
                str(payload.get("event_schema_version", "unknown")),
                "unknown",
                "unknown",
                "unknown",
                "unknown",
                "unknown",
                "UNKNOWN_SAFE_EVENT",
                13,
                "WARN",
                "UNKNOWN_DIAGNOSTIC_EVENT_VERSION",
                "diagnostics",
                "main_service",
                {
                    "original_diagnostic_schema_version": str(
                        payload.get("diagnostic_schema_version", "missing")
                    )[:64],
                    "original_event_schema_version": str(
                        payload.get("event_schema_version", "missing")
                    )[:64],
                },
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        return DiagnosticEvent(
            str(payload["diagnostic_event_id"]),
            int(payload["sequence"]),
            str(payload["diagnostic_schema_version"]),
            str(payload["event_schema_version"]),
            str(payload["timestamp_utc"]),
            str(payload["observed_timestamp_utc"]),
            str(payload["release_id"]),
            str(payload["build_id"]),
            str(payload["instance_id"]),
            str(payload["event_name"]),
            int(payload["severity_number"]),
            str(payload["severity_text"]),
            str(payload["safe_code"]),
            str(payload["component"]),
            str(payload["process_role"]),
            dict(payload["safe_attributes"]),
            payload.get("trace_id"),
            payload.get("span_id"),
            payload.get("workspace_ref"),
            payload.get("turn_id"),
            payload.get("operation_id"),
            payload.get("attempt_id"),
            payload.get("duration_ms"),
            payload.get("parent_span_id"),
        )
