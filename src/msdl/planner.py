from __future__ import annotations

from dataclasses import dataclass

from .models import Assignment, RepoFile, ServerProbe


@dataclass
class _Bucket:
    probe: ServerProbe
    files: list[RepoFile]
    total: int = 0

    def score(self) -> float:
        speed = max(self.probe.speed_bps, 1.0)
        return self.total / speed


def assign_files(files: list[RepoFile], probes: list[ServerProbe], reserve_bytes: int) -> list[Assignment]:
    if not probes:
        raise ValueError("at least one probed server is required")

    buckets = [_Bucket(probe=probe, files=[]) for probe in probes]
    for file in sorted(files, key=lambda item: item.size, reverse=True):
        eligible = [
            bucket
            for bucket in buckets
            if bucket.probe.free_bytes >= file.size + reserve_bytes
        ]
        if not eligible:
            raise ValueError(
                f"no server has enough temporary free space for {file.path} "
                f"({format_bytes(file.size)} plus reserve)"
            )
        bucket = min(eligible, key=lambda item: item.score())
        bucket.files.append(file)
        bucket.total += file.size

    return [
        Assignment(probe=bucket.probe, files=tuple(sorted(bucket.files, key=lambda item: item.path)))
        for bucket in buckets
        if bucket.files
    ]


def format_bytes(value: int | float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    number = float(value)
    for unit in units:
        if abs(number) < 1024.0 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024.0
