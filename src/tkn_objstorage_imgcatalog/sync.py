from __future__ import annotations

import hashlib
from copy import deepcopy

from .assets import NoteStore, Operation, Record, make_record
from .config import Config
from .errors import AppError, ConflictError
from .io import IMAGE_EXTENSIONS, atomic_bytes, now, sha256, within
from .notes import find_note, refresh_note, split_note, urls
from .storage import ObjectStore


def push(
    config: Config,
    selectors: list[str],
    operation: Operation,
    blobs: ObjectStore,
    *,
    overwrite: bool = False,
    yes: bool = False,
) -> list[Record]:
    store = NoteStore(config)
    plans = []
    remote_names = {r["relative_path"].casefold(): r["relative_path"] for r in blobs.list()}
    for record in store.select(selectors):
        if (
            remote_names.get(record["relative_path"].casefold(), record["relative_path"])
            != record["relative_path"]
        ):
            raise ConflictError(
                "Remote and local names differ only by case; no upload was performed."
            )
        local_hash = store.check_release(record)
        refresh_note(config, record, dry_run=True)
        relative = record["relative_path"]
        remote = blobs.get(relative)
        base = store.baseline(record)
        if remote:
            remote_hash = (
                base["sha256"]
                if base and base["etag"] == remote["etag"] and base["relative_path"] == relative
                else blobs.digest(relative, remote["etag"])
            )
            if remote_hash == local_hash:
                action = "unchanged"
            elif base and base["etag"] == remote["etag"]:
                action = "updated"
            elif overwrite:
                action = "replaced"
            else:
                raise ConflictError(
                    f"Remote differs without an unchanged baseline: {relative}; pull or explicitly --overwrite."
                )
        else:
            if base and not overwrite:
                raise ConflictError(
                    f"Remote was deleted since last sync: {relative}; use --overwrite to recreate."
                )
            action = "created"
        if action != "unchanged":
            blobs.validate_upload(store.release(record))
        plans.append((record, remote, action))
    changes = any(action != "unchanged" for _, _, action in plans)
    if changes and not operation.dry_run and not yes:
        if overwrite or blobs.public_access() is not False:
            raise AppError(
                "Public/unknown-access upload or explicit overwrite requires confirmation (--yes)."
            )
    result = []
    for record, remote, action in plans:
        if not operation.dry_run:
            store.check_release(record)
            operation.event(
                "push_prepared",
                asset_id=record["asset_id"],
                relative_path=record["relative_path"],
                sha256=record["release"]["sha256"],
                expected_etag=remote["etag"] if remote else None,
            )
            if action == "unchanged":
                current = blobs.get(record["relative_path"])
                if current is None or remote is None or current["etag"] != remote["etag"]:
                    raise ConflictError("Remote changed during synchronization.")
                remote = current
            else:
                remote = blobs.upload(
                    record["relative_path"], store.release(record), record, remote
                )
                store.check_release(record)
            store.set_baseline(record, remote)
            store.save(record, sync_status="synced")
            operation.event(
                "pushed", asset_id=record["asset_id"], etag=remote["etag"], status=action
            )
        result.append(
            {"asset_id": record["asset_id"], "path": record["relative_path"], "status": action}
        )
    return result


def pull(
    config: Config,
    selectors: list[str],
    operation: Operation,
    blobs: ObjectStore,
    *,
    overwrite: bool = False,
    yes: bool = False,
) -> list[Record]:
    store = NoteStore(config)
    records = {r["relative_path"]: r for r in store.assets()}
    selected = set(selectors)
    for selector in list(selected):
        for candidate in records.values():
            if selector == candidate["asset_id"]:
                selected.remove(selector)
                selected.add(candidate["relative_path"])
    remote_items = blobs.list()
    available = {r["relative_path"] for r in remote_items}
    if selected - available:
        raise AppError("Some selected objects do not exist in this configured scope.")
    plans = []
    for remote in remote_items:
        relative = remote["relative_path"]
        if selected and relative not in selected:
            continue
        target = within(config.data_root / "3_releases", relative)
        for name in records:
            if name.casefold() == relative.casefold() and name != relative:
                raise ConflictError("Remote and local names differ only by case.")
        record = records.get(relative)
        if record:
            refresh_note(config, record, dry_run=True)
        elif within(config.notes_root, relative + ".md").exists():
            raise ConflictError("An unrelated note occupies the download destination.")
        if target.exists() and record is None:
            raise ConflictError(f"Unmanaged local release exists: {relative}")
        local_hash = sha256(target) if target.is_file() else None
        if record and local_hash and local_hash != record["release"]["sha256"] and not overwrite:
            raise ConflictError(f"Local release was manually modified: {relative}.")
        base = store.baseline(record) if record else None
        remote_hash = (
            base["sha256"]
            if base and base["etag"] == remote["etag"] and base["relative_path"] == relative
            else blobs.digest(relative, remote["etag"])
        )
        action = (
            "unchanged"
            if local_hash == remote_hash
            else ("updated" if target.exists() else "created")
        )
        if action == "updated" and (not base or local_hash != base["sha256"]) and not overwrite:
            raise ConflictError(
                f"Local release differs from its synchronization baseline: {relative}; push or explicitly --overwrite."
            )
        plans.append((remote, record, action, remote_hash, local_hash))
    if overwrite and any(p[2] == "updated" for p in plans) and not operation.dry_run and not yes:
        raise AppError("Replacing local changes requires confirmation (--yes).")
    result = []
    for remote, record, action, remote_hash, local_hash in plans:
        relative = remote["relative_path"]
        if not operation.dry_run:
            target = within(config.data_root / "3_releases", relative)
            if (sha256(target) if target.exists() else None) != local_hash:
                raise ConflictError("Local image changed during download planning.")
            if action != "unchanged":
                content = blobs.download(relative, remote["etag"])
                if hashlib.sha256(content).hexdigest() != remote_hash:
                    raise ConflictError("Downloaded bytes did not match the inspected object.")
                if record:
                    record = deepcopy(record)
                    record["source"] = None
                    record["release"] = {
                        "sha256": remote_hash,
                        "bytes": len(content),
                        "entity_id": "urn:sha256:" + remote_hash,
                        "recipe": None,
                        "generated_at": now(),
                    }
                else:
                    record = make_record(
                        relative,
                        (urls(config, relative)[0] or relative),
                        source=None,
                        digest=remote_hash,
                        size=len(content),
                    )
                record["release"]["acquired_from"] = urls(config, relative)[0]
                operation.event("release_prepared", record=record, expected_etag=remote["etag"])
                atomic_bytes(target, content, expected=local_hash, create_only=local_hash is None)
            else:
                current = blobs.get(relative)
                if current is None or current["etag"] != remote["etag"]:
                    raise ConflictError("Remote changed during synchronization.")
            assert record is not None
            store.set_baseline(record, remote)
            store.save(record, sync_status="synced")
            operation.event(
                "pulled",
                asset_id=record["asset_id"],
                etag=remote["etag"],
                generated=record["release"]["entity_id"],
                status=action,
            )
        result.append(
            {"path": relative, "status": action, "asset_id": record["asset_id"] if record else None}
        )
    return result


def status(config: Config, *, blobs: ObjectStore | None = None) -> list[Record]:
    store = NoteStore(config)
    remote = {r["relative_path"]: r for r in blobs.list()} if blobs else {}
    result = []
    for record in store.assets():
        path = store.release(record)
        local = sha256(path) if path.exists() else None
        base = store.baseline(record)
        other = remote.pop(record["relative_path"], None)
        item = {
            "asset_id": record["asset_id"],
            "path": record["relative_path"],
            "local": (
                "missing"
                if local is None
                else "modified"
                if local != record["release"]["sha256"]
                else "valid"
            ),
            "source_available": record.get("source") is not None,
        }
        if blobs:
            item["remote"] = (
                "missing"
                if other is None
                else "untracked"
                if base is None
                else "unchanged"
                if other["etag"] == base["etag"]
                else "changed"
            )
            item["local_since_sync"] = (
                "unknown" if base is None else "unchanged" if local == base["sha256"] else "changed"
            )
        result.append(item)
    result.extend({"path": rel, "local": "missing", "remote": "remote_only"} for rel in remote)
    return result


def verify(config: Config, *, blobs: ObjectStore | None = None) -> list[Record]:
    store = NoteStore(config)
    result = []
    known = set()
    for record in store.assets():
        issues = []
        known.add(record["relative_path"])
        try:
            store.check_release(record)
        except AppError as exc:
            issues.append(str(exc))
        source = record.get("source")
        if source:
            original = within(config.data_root, source["path"])
            if not original.is_file() or sha256(original) != source["sha256"]:
                issues.append("Original missing or hash mismatch.")
        note = find_note(config, record)
        if not note.is_file():
            issues.append("Proxy note missing.")
        else:
            data, _ = split_note(note.read_text(encoding="utf-8"))
            if data.get("assetId") != record["asset_id"] or data.get("noteId") != record["note_id"]:
                issues.append("Proxy note identifiers do not match.")
            if data.get("sha256") != record["release"]["sha256"]:
                issues.append("Proxy note hash is stale; run notes refresh.")
        if blobs:
            remote = blobs.get(record["relative_path"])
            if (
                remote is None
                or blobs.digest(record["relative_path"], remote["etag"])
                != record["release"]["sha256"]
            ):
                issues.append("Remote image missing or different.")
        result.append(
            {
                "asset_id": record["asset_id"],
                "status": "failed" if issues else "valid",
                "issues": issues,
            }
        )
    for path in (config.data_root / "3_releases").rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and path.relative_to(config.data_root / "3_releases").as_posix() not in known
        ):
            result.append({"status": "failed", "issues": ["Unmanaged release."], "path": str(path)})
    return result
