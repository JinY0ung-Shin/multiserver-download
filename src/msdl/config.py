from __future__ import annotations

import tomllib
from pathlib import Path

from .models import ServerConfig, WorkerPlatform


def load_servers(path: Path) -> list[ServerConfig]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_servers = data.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ValueError(f"{path} must contain at least one [[servers]] entry")

    servers: list[ServerConfig] = []
    names: set[str] = set()
    for index, item in enumerate(raw_servers, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"server entry #{index} must be a table")
        name = str(item.get("name") or "").strip()
        ssh_target = str(item.get("ssh_target") or "").strip()
        platform = normalize_platform(item.get("platform", "linux"))
        roots = item.get("temp_roots", ["/tmp"])
        if not name:
            raise ValueError(f"server entry #{index} is missing name")
        if name in names:
            raise ValueError(f"duplicate server name: {name}")
        if not ssh_target:
            raise ValueError(f"server {name} is missing ssh_target")
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"server {name} must have non-empty temp_roots")
        temp_roots = tuple(normalize_temp_root(root, platform) for root in roots)
        servers.append(
            ServerConfig(
                name=name,
                ssh_target=ssh_target,
                temp_roots=temp_roots,
                platform=platform,
            )
        )
        names.add(name)
    return servers


def normalize_platform(value: object) -> WorkerPlatform:
    platform = str(value or "linux").strip().lower()
    if platform in {"linux", "posix"}:
        return "linux"
    if platform in {"windows", "win", "win32"}:
        return "windows"
    raise ValueError(f"unsupported server platform: {value}")


def normalize_temp_root(value: object, platform: WorkerPlatform) -> str:
    root = str(value).strip()
    if not root:
        raise ValueError("temp_roots cannot contain empty paths")
    if platform == "windows":
        normalized = root.replace("\\", "/")
        if len(normalized) == 3 and normalized[1:] == ":/":
            return normalized
        return normalized.rstrip("/") or normalized
    return root.rstrip("/") or "/"
