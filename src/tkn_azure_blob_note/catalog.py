from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from filelock import FileLock, Timeout

from . import __version__
from .config import Config
from .errors import AppError, ConflictError
from .io import (
    SCHEMA_VERSION,
    atomic_json,
    copy_verified,
    fingerprint,
    now,
    read_json,
    sha256,
    within,
)

Record = dict[str, Any]


class Catalog:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.data_root

    def assets(self) -> list[Record]:
        root = self.root / "catalog"
        records = []
        paths: set[str] = set()
        for path in sorted(root.glob("*.json")):
            within(root, path.name)
            item = read_json(path)
            try:
                for key in (
                    "asset_id",
                    "note_id",
                    "relative_path",
                    "note_path",
                    "created_at",
                    "updated_at",
                ):
                    if not isinstance(item.get(key), str):
                        raise ValueError
                if not isinstance(item.get("release"), dict):
                    raise ValueError
                release = item["release"]
                if (
                    not isinstance(release.get("sha256"), str)
                    or type(release.get("bytes")) is not int
                    or release["bytes"] < 0
                ):
                    raise ValueError
                if str(UUID(item["asset_id"])) != path.stem:
                    raise ValueError
                UUID(item["note_id"])
                relative = item["relative_path"]
                within(self.root / "releases", relative)
                within(self.config.notes_root, item["note_path"])
                digest = item["release"]["sha256"]
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ValueError
                if item.get("source") is not None:
                    source = item["source"]
                    if not isinstance(source, dict) or any(
                        not isinstance(source.get(key), str)
                        for key in ("path", "sha256", "ref", "entity_id")
                    ):
                        raise ValueError
                    if len(source["sha256"]) != 64 or any(
                        c not in "0123456789abcdef" for c in source["sha256"]
                    ):
                        raise ValueError
                    within(self.root, source["path"])
            except (KeyError, ValueError, TypeError) as exc:
                raise AppError(f"Invalid catalog record: {path.name}") from exc
            if relative.casefold() in paths:
                raise ConflictError("Catalog contains colliding release paths.")
            paths.add(relative.casefold())
            records.append(item)
        return records

    def select(self, selectors: list[str]) -> list[Record]:
        records = self.assets()
        if not selectors:
            return records
        result = []
        for selector in selectors:
            matches = [
                r
                for r in records
                if selector
                in {
                    r["asset_id"],
                    r["relative_path"],
                    r["note_path"],
                    str(self.release(r)),
                    str(self.config.notes_root / r["note_path"]),
                }
            ]
            if len(matches) != 1:
                raise AppError(f"Asset selector is missing or ambiguous: {selector}")
            if matches[0] not in result:
                result.append(matches[0])
        return result

    def release(self, record: Record) -> Path:
        return within(self.root / "releases", record["relative_path"])

    def save(self, record: Record) -> None:
        UUID(record["asset_id"])
        atomic_json(within(self.root / "catalog", record["asset_id"] + ".json"), record)

    def check_release(self, record: Record) -> str:
        path = self.release(record)
        if not path.is_file():
            raise AppError(
                f"Local release missing: {record['relative_path']}; use pull to restore."
            )
        actual = sha256(path)
        if actual != record["release"]["sha256"]:
            raise ConflictError(
                f"Release edited outside CLI: {record['relative_path']}; import the edit under a new name."
            )
        return actual

    def archive_release(self, record: Record) -> None:
        path = self.release(record)
        if path.exists():
            digest = sha256(path)
            copy_verified(path, within(self.root / "history", digest + "/" + path.name), digest)

    def target_key(self) -> str:
        return fingerprint(
            {
                "library": str(self.root),
                **{key: self.config.azure[key] for key in ("account_url", "container", "prefix")},
            }
        )

    def baseline(self, record: Record) -> Record | None:
        path = self.config.state_root / "sync" / self.target_key() / (record["asset_id"] + ".json")
        return read_json(path) if path.exists() else None

    def set_baseline(self, record: Record, remote: Record) -> None:
        value = {
            "schema_version": SCHEMA_VERSION,
            "asset_id": record["asset_id"],
            "relative_path": record["relative_path"],
            "sha256": record["release"]["sha256"],
            "etag": remote["etag"],
            "version_id": remote.get("version_id"),
            "synced_at": now(),
            "target": self.config.azure,
        }
        atomic_json(
            self.config.state_root / "sync" / self.target_key() / (record["asset_id"] + ".json"),
            value,
        )


def make_record(
    relative: str,
    identity: str,
    *,
    source: Record | None,
    digest: str,
    size: int,
    recipe: str | None = None,
) -> Record:
    asset_id = str(uuid5(NAMESPACE_URL, "azure-blob-note:" + identity))
    stamp = now()
    return {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset_id,
        "note_id": str(uuid5(UUID(asset_id), "note")),
        "relative_path": relative,
        "note_path": relative + ".md",
        "created_at": stamp,
        "updated_at": stamp,
        "source": source,
        "release": {
            "sha256": digest,
            "bytes": size,
            "entity_id": "urn:sha256:" + digest,
            "recipe": recipe,
            "generated_at": stamp,
        },
    }


class Operation(AbstractContextManager["Operation"]):
    """Serialize mutations and persist an incremental, crash-readable journal."""

    def __init__(self, config: Config, command: str, dry_run: bool):
        self.config = config
        self.dry_run = dry_run
        self.run_id = str(uuid4())
        self.lock: FileLock | None = None
        self.handler: logging.Handler | None = None
        self.value: Record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "command": command,
            "agent": {"name": "tkn-azure-blob-note", "version": __version__},
            "started_at": now(),
            "status": "running",
            "events": [],
            "config_fingerprint": fingerprint(config.values),
        }

    def __enter__(self) -> Operation:
        if not self.dry_run:
            self.config.data_root.mkdir(parents=True, exist_ok=True)
            self.lock = FileLock(str(self.config.data_root / ".operation.lock"), timeout=0)
            try:
                self.lock.acquire()
            except Timeout as exc:
                raise ConflictError("Another operation is using this data_root.") from exc
            try:
                log = self.config.state_root / "logs" / (self.run_id + ".log")
                log.parent.mkdir(parents=True, exist_ok=True)
                self.handler = logging.FileHandler(log, encoding="utf-8")
                self.handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%dT%H:%M:%S%z"
                    )
                )
                logging.getLogger("tkn_azure_blob_note").addHandler(self.handler)
                self.flush()
            except BaseException:
                self.lock.release()
                raise
        return self

    def flush(self) -> None:
        if not self.dry_run:
            atomic_json(self.config.data_root / "provenance" / (self.run_id + ".json"), self.value)
            atomic_json(self.config.state_root / "runs" / (self.run_id + ".json"), self.value)

    def event(self, action: str, **details: Any) -> None:
        self.value["events"].append({"action": action, "at": now(), **details})
        self.flush()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.value["status"] = "failed" if exc else "completed"
            self.value["ended_at"] = now()
            if exc:
                self.value["error_type"] = type(exc).__name__
            self.flush()
        finally:
            if self.handler:
                logging.getLogger("tkn_azure_blob_note").removeHandler(self.handler)
                self.handler.close()
            if self.lock:
                self.lock.release()
