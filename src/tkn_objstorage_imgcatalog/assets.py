from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from datetime import datetime
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
    fingerprint,
    now,
    read_json,
    sha256,
    within,
)

Record = dict[str, Any]


NOTE_SCHEMA_VERSION = "3.0.0"


def ensure_current_layout(config: Config) -> None:
    for name in ("catalog", "provenance"):
        folder = within(config.data_root, name)
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise AppError("Old storage layout found; run migrate --dry-run, then migrate.")


def validate_record(config: Config, item: Record) -> None:
    """Validate the in-memory image record used by notes and migration journals."""
    try:
        for key in (
            "asset_id",
            "note_id",
            "relative_path",
            "note_path",
            "created_at",
            "updated_at",
        ):
            if not isinstance(item[key], str) or not item[key]:
                raise ValueError
        for key in ("asset_id", "note_id"):
            if str(UUID(item[key])) != item[key]:
                raise ValueError
        within(config.data_root / "releases", item["relative_path"])
        within(config.notes_root, item["note_path"])
        release = item["release"]
        if (
            not isinstance(release, dict)
            or type(release["bytes"]) is not int
            or release["bytes"] < 0
        ):
            raise ValueError
        digests = [release["sha256"]]
        if release.get("recipe") is not None:
            digests.append(release["recipe"])
        source = item.get("source")
        if source is not None:
            if not isinstance(source, dict):
                raise ValueError
            for key in ("path", "sha256", "ref", "entity_id"):
                if not isinstance(source[key], str) or not source[key]:
                    raise ValueError
            if not source["path"].startswith("originals/"):
                raise ValueError
            within(config.data_root, source["path"])
            digests.append(source["sha256"])
            captured = source.get("captured_at", item["created_at"])
            if not isinstance(captured, str) or datetime.fromisoformat(captured).tzinfo is None:
                raise ValueError
        if any(
            not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
            for d in digests
        ):
            raise ValueError
        for stamp in (
            item["created_at"],
            item["updated_at"],
            release.get("generated_at", item["updated_at"]),
        ):
            if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is None:
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AppError("Invalid image record in note or migration input.") from exc


def record_from_note(config: Config, data: Any, relative_note: str) -> Record:
    version = data.get("schemaVersion")
    if isinstance(version, str) and version in {"1.0.0", "2.0.0"}:
        raise AppError("Legacy note schema requires migrate and its legacy catalog.")
    if data.get("schemaVersion") != NOTE_SCHEMA_VERSION:
        raise AppError("Unsupported note schema version.")
    # Obsidian/user edits may leave ISO timestamps unquoted in YAML.
    data = dict(data)
    for key in ("created", "updated", "sourceCapturedAt", "releaseGeneratedAt"):
        if isinstance(data.get(key), datetime):
            data[key] = data[key].isoformat()
    try:
        ref = data["releaseRef"]
        if not isinstance(ref, str) or not ref.startswith("releases/"):
            raise ValueError
        if type(data["sourceAvailable"]) is not bool:
            raise ValueError
        source = None
        if data["sourceAvailable"]:
            source = {
                "path": data["originalRef"],
                "ref": data["sourceRef"],
                "sha256": data["sourceSha256"],
                "captured_at": data["sourceCapturedAt"],
                "entity_id": "urn:sha256:" + data["sourceSha256"],
            }
        elif any(
            data.get(k) is not None
            for k in ("originalRef", "sourceRef", "sourceSha256", "sourceCapturedAt")
        ):
            raise ValueError
        item: Record = {
            "schema_version": SCHEMA_VERSION,
            "asset_id": data["assetId"],
            "note_id": data["noteId"],
            "relative_path": ref[len("releases/") :],
            "note_path": relative_note,
            "created_at": data["created"],
            "updated_at": data["updated"],
            "source": source,
            "release": {
                "sha256": data["sha256"],
                "bytes": data["bytes"],
                "recipe": data["conversionRecipe"],
                "generated_at": data["releaseGeneratedAt"],
                "entity_id": "urn:sha256:" + data["sha256"],
            },
        }
        if data.get("acquiredFrom") is not None:
            item["release"]["acquired_from"] = data["acquiredFrom"]
        validate_record(config, item)
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(
            "Invalid image Frontmatter; required management fields are missing or invalid."
        ) from exc
    return item


def scan_notes(config: Config) -> list[Record]:
    from .notes import BEGIN, LEGACY_BEGIN, split_note

    records = []
    asset_ids: set[str] = set()
    note_ids: set[str] = set()
    paths: set[str] = set()
    for path in sorted(config.notes_root.rglob("*.md")):
        relative = path.relative_to(config.notes_root).as_posix()
        within(config.notes_root, relative)
        data, body = split_note(path.read_text(encoding="utf-8"))
        if (
            not any(k in data for k in ("assetId", "noteId", "releaseRef"))
            and BEGIN not in body
            and LEGACY_BEGIN not in body
        ):
            continue
        record = record_from_note(config, data, relative)
        if record["asset_id"] in asset_ids or record["note_id"] in note_ids:
            raise ConflictError("Multiple notes use the same asset/note ID.")
        if record["relative_path"].casefold() in paths:
            raise ConflictError("Multiple notes use colliding release paths.")
        asset_ids.add(record["asset_id"])
        note_ids.add(record["note_id"])
        paths.add(record["relative_path"].casefold())
        records.append(record)
    return records


class NoteStore:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.data_root

    def assets(self) -> list[Record]:
        ensure_current_layout(self.config)
        return scan_notes(self.config)

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

    def save(self, record: Record, *, sync_status: str | None = None) -> None:
        from .notes import refresh_note

        validate_record(self.config, record)
        refresh_note(self.config, record, sync_status=sync_status)

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

    def target_key(self) -> str:
        return fingerprint(
            {
                "library": str(self.root),
                **self.config.target_identity,
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
            "target": {"provider": self.config.provider, **self.config.target_identity},
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
    # This historical namespace is part of the durable ID contract, not the product name.
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
            "agent": {"name": "tkn-objstorage-imgcatalog", "version": __version__},
            "started_at": now(),
            "status": "running",
            "events": [],
            "source_id": config.select_source().source_id,
            "config_fingerprint": fingerprint(config.source_values),
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
                logging.getLogger("tkn_objstorage_imgcatalog").addHandler(self.handler)
                self.flush()
            except BaseException:
                self.lock.release()
                raise
        return self

    def flush(self) -> None:
        if not self.dry_run:
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
                logging.getLogger("tkn_objstorage_imgcatalog").removeHandler(self.handler)
                self.handler.close()
            if self.lock:
                self.lock.release()
