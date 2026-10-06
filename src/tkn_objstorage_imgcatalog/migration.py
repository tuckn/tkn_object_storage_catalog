from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from .assets import (
    NOTE_SCHEMA_VERSION,
    NoteStore,
    Operation,
    Record,
    record_from_note,
    scan_notes,
    validate_record,
)
from .config import Config
from .errors import AppError, ConflictError
from .io import atomic_bytes, check_schema, read_json, sha256, within
from .notes import BEGIN, LEGACY_BEGIN, find_note, render_note, split_note


def read_input(path: Path) -> tuple[Record, bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise AppError("Cannot read a legacy migration input.") from exc
    if not isinstance(value, dict):
        raise AppError("Expected a JSON object in a legacy migration input.")
    check_schema(value, "Legacy migration input")
    return value, payload


def legacy_files(config: Config, name: str) -> list[Path]:
    root = within(config.data_root, name)
    if not root.exists():
        return []
    if not root.is_dir():
        raise AppError(f"Legacy {name} must be a directory.")
    result = []
    for path in sorted(root.iterdir()):
        within(root, path.name)
        if not path.is_file() or path.suffix != ".json":
            raise AppError(f"Unexpected entry in legacy {name}; migration left it unchanged.")
        result.append(path)
    return result


def migrate(config: Config, operation: Operation) -> Record:
    """Preflight everything, retain rollback inputs, then retire enumerated old files."""
    old_records = legacy_files(config, "catalog")
    old_journals = legacy_files(config, "provenance")
    # Each write/removal carries the exact bytes seen in the preflight.
    writes: list[tuple[Path, bytes | None, bytes]] = []
    removals: list[tuple[Path, bytes]] = []
    images: list[tuple[Path, str]] = []
    matched_notes: set[Path] = set()
    records: list[Record] = []
    changed_notes = 0
    before: bytes | None
    for path in old_records:
        record, record_bytes = read_input(path)
        validate_record(config, record)
        if path.stem != record["asset_id"]:
            raise AppError("Legacy record filename does not match its asset ID.")
        note = find_note(config, record)
        if not note.is_file():
            raise AppError("A legacy asset note is missing; restore it before migration.")
        before = note.read_bytes()
        data, _ = split_note(before.decode("utf-8"))
        if data.get("assetId") != record["asset_id"] or data.get("noteId") != record["note_id"]:
            raise ConflictError("Legacy note identifiers differ from the migration record.")
        record["note_path"] = note.relative_to(config.notes_root).as_posix()
        if data.get("schemaVersion") == NOTE_SCHEMA_VERSION:
            # A partially completed migration may be resumed, but cannot overwrite later edits.
            current = record_from_note(config, data, record["note_path"])
            for key in (
                "asset_id",
                "note_id",
                "relative_path",
                "created_at",
                "source",
                "release",
            ):
                expected = record.get(key)
                actual = current.get(key)
                if (
                    key in {"source", "release"}
                    and isinstance(expected, dict)
                    and isinstance(actual, dict)
                ):
                    if any(actual.get(k) != v for k, v in expected.items()):
                        raise ConflictError("Already migrated note differs from the old record.")
                elif actual != expected:
                    raise ConflictError("Already migrated note differs from the old record.")
        release = within(config.data_root / "releases", record["relative_path"])
        images.append((release, record["release"]["sha256"]))
        if record.get("source"):
            images.append(
                (within(config.data_root, record["source"]["path"]), record["source"]["sha256"])
            )
        rendered = render_note(config, record, note, before.decode("utf-8")).encode("utf-8")
        # Prove the new note contains all fields needed to operate without a JSON catalog.
        new_data, _ = split_note(rendered.decode("utf-8"))
        for key in (
            "created",
            "conversionRecipe",
            "releaseGeneratedAt",
            "sourceCapturedAt",
            "acquiredFrom",
        ):
            if key in data and data[key] != new_data.get(key):
                raise ConflictError(
                    "A new management field conflicts with an existing note property."
                )
        migrated = record_from_note(config, new_data, record["note_path"])
        records.append(migrated)
        matched_notes.add(note)
        if before != rendered:
            writes.append((note, before, rendered))
            changed_notes += 1
        removals.append((path, record_bytes))
    for path in sorted(config.notes_root.rglob("*.md")):
        within(config.notes_root, path.relative_to(config.notes_root).as_posix())
        if path in matched_notes:
            continue
        data, body = split_note(path.read_text(encoding="utf-8"))
        if (
            not any(k in data for k in ("assetId", "noteId", "releaseRef"))
            and BEGIN not in body
            and LEGACY_BEGIN not in body
        ):
            continue
        records.append(
            record_from_note(config, data, path.relative_to(config.notes_root).as_posix())
        )
    for key in ("asset_id", "note_id", "relative_path"):
        values = [r[key].casefold() for r in records]
        if len(values) != len(set(values)):
            raise ConflictError("Migration inputs contain duplicate IDs or release paths.")
    copied_runs = 0
    for path in old_journals:
        journal, payload = read_input(path)
        try:
            if str(UUID(journal["run_id"])) != path.stem:
                raise ValueError
            if journal["status"] not in {"running", "failed", "completed"} or not isinstance(
                journal["events"], list
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise AppError("Invalid legacy execution record; migration stopped.") from exc
        target = within(config.state_root, "runs/" + path.name)
        if target.exists():
            if read_json(target) != journal:
                raise ConflictError(
                    "A different execution record has the same run ID in state/runs."
                )
        else:
            writes.append((target, None, payload))
            copied_runs += 1
        removals.append((path, payload))
    for path, digest in images:
        if not path.is_file() or sha256(path) != digest:
            raise ConflictError("An image or original differs from its migration record.")
    result: Record = {
        "status": "planned" if operation.dry_run else "migrated",
        "assets": len(old_records),
        "notes_updated": changed_notes,
        "runs_copied": copied_runs,
        "legacy_records_retired": len(removals),
    }
    if not removals and not writes:
        return {**result, "status": "unchanged"}
    if operation.dry_run:
        return result
    backup = within(config.state_root, "migrations/" + operation.run_id)
    # Copy only the inputs of this migration; do not archive image bytes or unrelated folders.
    for path, payload in removals:
        target = within(backup, "data/" + path.relative_to(config.data_root).as_posix())
        atomic_bytes(target, payload, create_only=True)
    for path, before, _ in writes:
        if before is not None:
            target = within(backup, "notes/" + path.relative_to(config.notes_root).as_posix())
            atomic_bytes(target, before, create_only=True)
    operation.event(
        "migration_prepared", assets=len(old_records), notes=changed_notes, runs=copied_runs
    )
    applied: list[tuple[Path, bytes | None, bytes]] = []
    removed: list[tuple[Path, bytes]] = []
    try:
        for path, before, after in writes:
            within(
                config.notes_root if path.is_relative_to(config.notes_root) else config.state_root,
                path.relative_to(
                    config.notes_root
                    if path.is_relative_to(config.notes_root)
                    else config.state_root
                ).as_posix(),
            )
            atomic_bytes(
                path,
                after,
                expected=hashlib.sha256(before).hexdigest() if before is not None else None,
                create_only=before is None,
            )
            applied.append((path, before, after))
        if sorted(scan_notes(config), key=lambda r: r["asset_id"]) != sorted(
            records, key=lambda r: r["asset_id"]
        ):
            raise ConflictError("Notes changed during migration.")
        for path, digest in images:
            if not path.is_file() or sha256(path) != digest:
                raise ConflictError("An image changed during migration.")
        for path, before in removals:
            within(config.data_root, path.relative_to(config.data_root).as_posix())
            if not path.is_file() or path.read_bytes() != before:
                raise ConflictError("A legacy input changed during migration.")
            path.unlink()
            removed.append((path, before))
        # Validate the new active layout before recording success.
        NoteStore(config).assets()
        operation.event("migration_completed", **result)
    except BaseException:
        rollback_errors = []
        for path, payload in reversed(removed):
            try:
                atomic_bytes(path, payload, create_only=True)
            except (AppError, OSError) as exc:
                rollback_errors.append(type(exc).__name__)
        for path, before, after in reversed(applied):
            try:
                digest = hashlib.sha256(after).hexdigest()
                if before is None:
                    if path.is_file() and sha256(path) == digest:
                        path.unlink()
                    else:
                        raise ConflictError("A migrated file changed during rollback.")
                else:
                    atomic_bytes(path, before, expected=digest)
            except (AppError, OSError) as exc:
                rollback_errors.append(type(exc).__name__)
        if rollback_errors:
            raise AppError(
                "Migration rollback was interrupted; restore inputs from state/migrations before retrying."
            ) from None
        raise
    for name in ("catalog", "provenance"):
        folder = within(config.data_root, name)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return result
