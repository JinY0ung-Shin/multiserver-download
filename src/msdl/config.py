from __future__ import annotations

import tomllib
from pathlib import Path

from .models import ServerConfig


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
        roots = item.get("temp_roots", ["/tmp"])
        if not name:
            raise ValueError(f"server entry #{index} is missing name")
        if name in names:
            raise ValueError(f"duplicate server name: {name}")
        if not ssh_target:
            raise ValueError(f"server {name} is missing ssh_target")
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"server {name} must have non-empty temp_roots")
        temp_roots = tuple(str(root).rstrip("/") or "/" for root in roots)
        servers.append(ServerConfig(name=name, ssh_target=ssh_target, temp_roots=temp_roots))
        names.add(name)
    return servers
