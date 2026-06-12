from msdl.models import RepoFile, ServerConfig, ServerProbe
from msdl.planner import assign_files


def probe(name: str, speed: float, free: int = 10_000) -> ServerProbe:
    return ServerProbe(
        config=ServerConfig(name=name, ssh_target=name, temp_roots=("/tmp",)),
        temp_root="/tmp",
        temp_dir=f"/tmp/{name}",
        free_bytes=free,
        speed_bps=speed,
    )


def test_assign_files_prefers_faster_servers():
    files = [
        RepoFile("a", 100),
        RepoFile("b", 100),
        RepoFile("c", 100),
        RepoFile("d", 100),
    ]

    assignments = assign_files(files, [probe("fast", 3), probe("slow", 1)], reserve_bytes=0)
    totals = {assignment.probe.config.name: assignment.total_bytes for assignment in assignments}

    assert totals["fast"] > totals["slow"]


def test_assign_files_rejects_file_that_does_not_fit_any_server():
    files = [RepoFile("huge", 1000)]

    try:
        assign_files(files, [probe("small", 1, free=100)], reserve_bytes=0)
    except ValueError as exc:
        assert "no server has enough" in str(exc)
    else:
        raise AssertionError("expected ValueError")
