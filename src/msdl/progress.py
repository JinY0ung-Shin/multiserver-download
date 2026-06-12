from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Assignment
from .planner import format_bytes


ACTIVE_STATES = {"downloading", "transferring"}
COMPLETE_STATES = {"done", "skipped"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProgressTracker:
    def __init__(self, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self._payload = payload
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        path: Path,
        repo_id: str,
        revision: str,
        target_dir: Path,
        assignments: list[Assignment],
    ) -> "ProgressTracker":
        files: dict[str, dict[str, Any]] = {}
        servers: dict[str, dict[str, Any]] = {}

        for assignment in assignments:
            server_name = assignment.probe.config.name
            servers[server_name] = {
                "total_files": len(assignment.files),
                "total_bytes": assignment.total_bytes,
                "completed_files": 0,
                "completed_bytes": 0,
                "failed_files": 0,
                "current": None,
                "current_state": None,
            }
            for file in assignment.files:
                files[file.path] = {
                    "size": file.size,
                    "server": server_name,
                    "status": "pending",
                    "updated_at": None,
                    "error": None,
                }

        now = utc_now()
        payload: dict[str, Any] = {
            "job_id": path.parent.name,
            "repo_id": repo_id,
            "revision": revision,
            "target_dir": str(target_dir),
            "state": "planned",
            "started_at": now,
            "updated_at": now,
            "totals": {
                "files": len(files),
                "bytes": sum(file["size"] for file in files.values()),
                "completed_files": 0,
                "completed_bytes": 0,
                "failed_files": 0,
                "percent": 0.0,
            },
            "servers": servers,
            "files": files,
        }
        tracker = cls(path, payload)
        tracker._write_locked()
        return tracker

    def set_state(self, state: str) -> None:
        with self._lock:
            self._payload["state"] = state
            self._payload["updated_at"] = utc_now()
            self._recalculate_locked()
            self._write_locked()

    def update_file(self, file_path: str, status: str, error: str | None = None) -> str:
        with self._lock:
            item = self._payload["files"][file_path]
            item["status"] = status
            item["updated_at"] = utc_now()
            item["error"] = error
            self._payload["updated_at"] = item["updated_at"]
            self._recalculate_locked()
            self._write_locked()
            return format_status_summary(self._payload)

    def summary(self) -> str:
        with self._lock:
            return format_status_summary(self._payload)

    def _recalculate_locked(self) -> None:
        totals = self._payload["totals"]
        servers = self._payload["servers"]
        for server in servers.values():
            server["completed_files"] = 0
            server["completed_bytes"] = 0
            server["failed_files"] = 0
            server["current"] = None
            server["current_state"] = None

        completed_files = 0
        completed_bytes = 0
        failed_files = 0

        for path, item in self._payload["files"].items():
            status = item["status"]
            server = servers[item["server"]]
            if status in COMPLETE_STATES:
                completed_files += 1
                completed_bytes += item["size"]
                server["completed_files"] += 1
                server["completed_bytes"] += item["size"]
            elif status == "failed":
                failed_files += 1
                server["failed_files"] += 1
            elif status in ACTIVE_STATES:
                server["current"] = path
                server["current_state"] = status

        totals["completed_files"] = completed_files
        totals["completed_bytes"] = completed_bytes
        totals["failed_files"] = failed_files
        total_bytes = max(totals["bytes"], 1)
        totals["percent"] = round((completed_bytes / total_bytes) * 100, 2)

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        tmp_path.write_text(
            json.dumps(self._payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)


def load_status(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_status_file(target_dir: Path, job_id: str | None = None) -> Path:
    base = target_dir / ".msdl"
    if job_id:
        path = base / job_id / "status.json"
        if not path.exists():
            raise FileNotFoundError(f"status file not found: {path}")
        return path

    candidates = [path for path in base.glob("*/status.json") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"no status files found under {base}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def format_status_report(payload: dict[str, Any]) -> str:
    lines = [
        format_status_summary(payload),
        f"job: {payload['job_id']}",
        f"repo: {payload['repo_id']}",
        f"revision: {payload['revision']}",
        f"target: {payload['target_dir']}",
        f"updated: {payload['updated_at']}",
        "servers:",
    ]
    for name, server in sorted(payload["servers"].items()):
        line = (
            f"  {name}: {server['completed_files']}/{server['total_files']} files, "
            f"{format_bytes(server['completed_bytes'])}/{format_bytes(server['total_bytes'])}"
        )
        if server["failed_files"]:
            line += f", failed={server['failed_files']}"
        if server["current"]:
            line += f", {server['current_state']}={server['current']}"
        lines.append(line)

    failed = [
        (path, item)
        for path, item in sorted(payload["files"].items())
        if item["status"] == "failed"
    ]
    if failed:
        lines.append("failed files:")
        for path, item in failed[:10]:
            lines.append(f"  {path}: {item['error']}")
    return "\n".join(lines)


def format_status_summary(payload: dict[str, Any]) -> str:
    totals = payload["totals"]
    return (
        f"{payload['state']}: "
        f"{totals['completed_files']}/{totals['files']} files, "
        f"{format_bytes(totals['completed_bytes'])}/{format_bytes(totals['bytes'])} "
        f"({totals['percent']:.2f}%)"
    )
