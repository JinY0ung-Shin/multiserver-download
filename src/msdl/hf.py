from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .models import RepoFile


SAVE_PATH_ENV = "MULTISERVER_DOWNLOAD_SAVE_PATH"


def get_hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")


def target_dir_for(repo_id: str, save_root: Path) -> Path:
    parts = repo_id.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("repo_id must use the Hugging Face <org>/<model> form")
    return save_root / parts[0] / parts[1]


def validate_repo_file_path(path: str) -> None:
    pure = Path(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe repo file path: {path}")


def list_repo_files(
    repo_id: str,
    revision: str,
    includes: list[str],
    excludes: list[str],
    token: str | None,
) -> list[RepoFile]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is required; run `pip install -e .`") from exc

    api = HfApi(token=token)
    info = api.repo_info(repo_id=repo_id, revision=revision, files_metadata=True)

    files: list[RepoFile] = []
    for sibling in info.siblings:
        path = sibling.rfilename
        validate_repo_file_path(path)
        if includes and not any(fnmatch.fnmatch(path, pattern) for pattern in includes):
            continue
        if excludes and any(fnmatch.fnmatch(path, pattern) for pattern in excludes):
            continue
        size = getattr(sibling, "size", None)
        if size is None:
            lfs = getattr(sibling, "lfs", None) or {}
            size = lfs.get("size") if isinstance(lfs, dict) else None
        files.append(RepoFile(path=path, size=int(size or 0), etag=getattr(sibling, "blob_id", None)))

    if not files:
        raise ValueError("no files matched the requested include/exclude filters")
    return sorted(files, key=lambda item: item.path)
