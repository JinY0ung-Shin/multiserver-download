from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from pathlib import PurePosixPath

from .models import RepoFile


SAVE_PATH_ENV = "MULTISERVER_DOWNLOAD_SAVE_PATH"
INSECURE_SKIP_TLS_VERIFY_ENV = "MULTISERVER_DOWNLOAD_INSECURE_SKIP_TLS_VERIFY"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_CHARS = set('<>:"|?*')


def get_hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def target_dir_for(repo_id: str, save_root: Path) -> Path:
    namespace, name = validate_repo_id(repo_id)
    return save_root / namespace / name


def validate_repo_id(repo_id: str) -> tuple[str, str]:
    if "\\" in repo_id:
        raise ValueError("repo_id must use the Hugging Face <org>/<model> form")
    parts = repo_id.split("/")
    if (
        len(parts) != 2
        or not parts[0]
        or not parts[1]
        or any(part in {".", ".."} for part in parts)
        or any(PurePosixPath(part).is_absolute() for part in parts)
    ):
        raise ValueError("repo_id must use the Hugging Face <org>/<model> form")
    return parts[0], parts[1]


def repo_file_parts(path: str) -> tuple[str, ...]:
    validate_repo_file_path(path)
    return tuple(PurePosixPath(path).parts)


def local_path_for_repo_file(base_dir: Path, path: str) -> Path:
    parts = repo_file_parts(path)
    if os.name == "nt":
        validate_windows_repo_file_path(path)
    return base_dir.joinpath(*parts)


def validate_repo_file_path(path: str) -> None:
    if "\\" in path:
        raise ValueError(f"unsafe repo file path: {path}")
    pure = PurePosixPath(path)
    parts = path.split("/")
    if (
        not path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe repo file path: {path}")


def validate_windows_repo_file_path(path: str) -> None:
    for part in repo_file_parts(path):
        upper_stem = part.split(".", 1)[0].upper()
        if (
            upper_stem in _WINDOWS_RESERVED_NAMES
            or part[-1:] in {" ", "."}
            or any(char in _WINDOWS_INVALID_CHARS or ord(char) < 32 for char in part)
        ):
            raise ValueError(f"repo file path is not valid on Windows: {path}")


def list_repo_files(
    repo_id: str,
    revision: str,
    includes: list[str],
    excludes: list[str],
    token: str | None,
    insecure_skip_tls_verify: bool = False,
) -> list[RepoFile]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("huggingface_hub is required; run `pip install -e .`") from exc

    configure_hf_http_client(insecure_skip_tls_verify)
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
        if size is None:
            raise RuntimeError(f"missing size metadata for repo file: {path}")
        files.append(RepoFile(path=path, size=int(size or 0), etag=getattr(sibling, "blob_id", None)))

    if not files:
        raise ValueError("no files matched the requested include/exclude filters")
    return sorted(files, key=lambda item: item.path)


def configure_hf_http_client(insecure_skip_tls_verify: bool) -> None:
    if not insecure_skip_tls_verify:
        return
    try:
        import httpx
        from huggingface_hub.utils import set_client_factory
        from huggingface_hub.utils._http import hf_request_event_hook
    except ImportError as exc:
        raise RuntimeError("huggingface_hub and httpx are required for insecure TLS mode") from exc

    set_client_factory(
        lambda: httpx.Client(
            event_hooks={"request": [hf_request_event_hook]},
            follow_redirects=True,
            timeout=None,
            verify=False,
        )
    )
