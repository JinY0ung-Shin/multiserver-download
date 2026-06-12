from pathlib import Path

from msdl.models import Assignment, RepoFile, ServerConfig, ServerProbe
from msdl.progress import (
    ProgressTracker,
    find_status_file,
    format_status_report,
    load_status,
)


def test_progress_tracker_writes_status(tmp_path):
    probe = ServerProbe(
        config=ServerConfig("ext1", "user@ext1", ("/tmp",)),
        temp_root="/tmp",
        temp_dir="/tmp/msdl/job",
        free_bytes=10_000,
        speed_bps=100,
    )
    assignment = Assignment(probe=probe, files=(RepoFile("model.bin", 100),))
    status_path = tmp_path / "target" / ".msdl" / "job123" / "status.json"

    tracker = ProgressTracker.create(
        status_path,
        repo_id="org/model",
        revision="main",
        target_dir=tmp_path / "target",
        assignments=[assignment],
    )
    tracker.set_state("running")
    tracker.update_file("model.bin", "done")
    tracker.set_state("complete")

    payload = load_status(status_path)
    assert payload["state"] == "complete"
    assert payload["totals"]["completed_files"] == 1
    assert payload["totals"]["completed_bytes"] == 100
    assert find_status_file(tmp_path / "target") == status_path
    assert "complete: 1/1 files" in format_status_report(payload)
