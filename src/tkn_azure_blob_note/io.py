from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import AppError, ConflictError

SCHEMA_VERSION = "1.0.0"
IMAGE_EXTENSIONS = frozenset(
    {
        ".apng",
        ".avif",
        ".bmp",
        ".gif",
        ".ico",
        ".jpg",
        ".jpeg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def safe_relative(value: str) -> str:
    """Reject paths that alias or escape on Windows, even when running on Unix."""
    if not value or value.startswith("/") or "\\" in value:
        raise AppError(f"Not a portable relative path: {value!r}")
    for part in value.split("/"):
        if (
            part in {"", ".", ".."}
            or part[-1:] in {" ", "."}
            or re.search(r'[<>:"|?*\x00-\x1f]', part)
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)
        ):
            raise AppError(f"Not a portable relative path: {value!r}")
    return PurePosixPath(value).as_posix()


def within(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise AppError("Managed directory must not be a link.")
    resolved_root = root.resolve()
    target = root.joinpath(*relative.split("/"))
    try:
        target.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise AppError("Path escapes its configured root (possibly through a link).") from exc
    # No linked files/directories inside application-owned roots.
    current = target
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise AppError("Linked files and directories are not managed automatically.")
        current = current.parent
    return target


def check_schema(value: dict[str, Any], label: str) -> None:
    version = value.get("schema_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise AppError(f"{label}: schema_version must be a quoted MAJOR.MINOR.PATCH string.")
    major, minor, _ = map(int, version.split("."))
    if major != 1 or minor != 0:
        raise AppError(f"{label}: unsupported schema {version}; supported schema is 1.0.x.")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise AppError(f"Cannot read JSON record: {path.name}") from exc
    if not isinstance(value, dict):
        raise AppError(f"Expected an object in {path.name}.")
    check_schema(value, path.name)
    return value


def install_new(temp: Path, target: Path) -> None:
    """Install a completed file without replacing a concurrently created destination."""
    try:
        if os.name == "nt":
            os.rename(temp, target)
        else:
            os.link(temp, target)
    except FileExistsError as exc:
        raise ConflictError(f"Destination appeared during write: {target.name}") from exc


def atomic_bytes(
    path: Path, content: bytes, *, expected: str | None = None, create_only: bool = False
) -> bool:
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".azure-blob-note-", suffix=".tmp", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None and (not path.is_file() or sha256(path) != expected):
            raise ConflictError(f"File changed during this operation: {path.name}")
        if create_only:
            install_new(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def atomic_json(path: Path, value: dict[str, Any]) -> bool:
    return atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def copy_verified(source: Path, target: Path, digest: str | None = None) -> str:
    digest = digest or sha256(source)
    if target.exists():
        if not target.is_file() or sha256(target) != digest:
            raise ConflictError(f"Destination already contains different data: {target.name}")
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".azure-blob-note-", suffix=".tmp", dir=target.parent)
    temp = Path(temporary)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if sha256(temp) != digest or sha256(source) != digest:
            raise ConflictError("Source changed while copying; source was retained.")
        if target.exists():
            raise ConflictError(f"Destination appeared while copying: {target.name}")
        install_new(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return digest


def scan_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            candidate = Path(parent) / name
            within(root, candidate.relative_to(root).as_posix())
        result.extend(Path(parent) / name for name in files)
    return sorted(result)


def image_files(path: Path) -> list[Path]:
    if not path.exists():
        raise AppError(f"Input does not exist: {path}")
    files = [path] if path.is_file() else scan_files(path)
    return [p for p in files if p.suffix.lower() in IMAGE_EXTENSIONS]
