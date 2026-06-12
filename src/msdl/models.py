from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int
    etag: str | None = None


@dataclass(frozen=True)
class ServerConfig:
    name: str
    ssh_target: str
    temp_roots: tuple[str, ...]


@dataclass(frozen=True)
class ServerProbe:
    config: ServerConfig
    temp_root: str
    temp_dir: str
    free_bytes: int
    speed_bps: float


@dataclass(frozen=True)
class Assignment:
    probe: ServerProbe
    files: tuple[RepoFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(file.size for file in self.files)
