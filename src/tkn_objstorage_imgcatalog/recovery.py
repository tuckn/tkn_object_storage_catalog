from __future__ import annotations

from datetime import datetime

from .assets import NoteStore, Operation, Record
from .config import Config
from .errors import ConflictError
from .io import read_json, sha256, within


def release_time(record: Record) -> datetime:
    # Recovery ordering concerns image versions, not edits to their notes.
    return datetime.fromisoformat(record["release"].get("generated_at", record["updated_at"]))


def recover(config: Config, operation: Operation) -> list[Record]:
    store = NoteStore(config)
    existing = {r["asset_id"]: r for r in store.assets()}
    candidates: dict[str, Record] = {}
    for path in (config.state_root / "runs").glob("*.json"):
        journal = read_json(path)
        if journal["run_id"] == operation.run_id or journal["status"] == "completed":
            continue
        for event in journal["events"]:
            if event["action"] == "release_prepared" and "record" in event:
                record = event["record"]
                previous = candidates.get(record["asset_id"])
                if previous is None or release_time(previous) < release_time(record):
                    candidates[record["asset_id"]] = record
    result = []
    for asset_id, prepared in candidates.items():
        current = existing.get(asset_id)
        if current and release_time(current) > release_time(prepared):
            continue
        target = store.release(prepared)
        if not target.exists() or sha256(target) != prepared["release"]["sha256"]:
            result.append(
                {"asset_id": asset_id, "status": "skipped", "reason": "prepared_bytes_not_present"}
            )
            continue
        source = prepared.get("source")
        if source:
            original = within(config.data_root, source["path"])
            if not original.exists() or sha256(original) != source["sha256"]:
                raise ConflictError("Original does not match the recovery record.")
        if not operation.dry_run:
            store.save(prepared)
            operation.event("recovered", asset_id=asset_id)
        result.append({"asset_id": asset_id, "status": "recovered"})
    return result
