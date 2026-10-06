from __future__ import annotations

import os
import re
from datetime import datetime
from io import StringIO
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote

from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .assets import NOTE_SCHEMA_VERSION, NoteStore, Record
from .config import Config, resource
from .errors import AppError, ConflictError
from .io import atomic_bytes, now, sha256, within
from .note_template import format_frontmatter, note_template
from .yaml_dates import note_yaml, quote_dates

# Keep the persisted markers stable across CLI/package renames.
BEGIN = "<!-- object-storage-catalog:begin -->"
END = "<!-- object-storage-catalog:end -->"
LEGACY_BEGIN = "<!-- azure-blob-note:begin -->"
LEGACY_END = "<!-- azure-blob-note:end -->"


def split_note(text: str) -> tuple[CommentedMap, str]:
    yaml = note_yaml()
    match = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return CommentedMap(), text
    try:
        data = yaml.load(match.group(1))
    except YAMLError as exc:
        raise AppError("Invalid note Frontmatter; the note was retained.") from exc
    if not isinstance(data, CommentedMap):
        raise AppError("Note Frontmatter must be a mapping.")
    return data, text[match.end() :]


def serialize(data: CommentedMap, body: str) -> str:
    yaml = note_yaml()
    stream = StringIO()
    yaml.dump(quote_dates(data), stream)
    return "---\n" + stream.getvalue() + "---\n\n" + body.lstrip("\r\n")


def urls(config: Config, relative: str) -> tuple[str | None, str | None]:
    storage = config.storage
    name = "/".join(part for part in (storage["prefix"], relative) if part)
    object_url = None
    if config.provider == "azure":
        if storage["account_url"] and storage["container"]:
            object_url = f"{storage['account_url']}/{quote(storage['container'], safe='$')}/{quote(name, safe='/')}"
    elif storage["bucket"]:
        endpoint = storage["endpoint_url"]
        if config.provider == "s3" and not endpoint:
            region = storage["region"]
            domain = "amazonaws.com.cn" if region and region.startswith("cn-") else "amazonaws.com"
            endpoint = f"https://s3.{region}.{domain}" if region else "https://s3.amazonaws.com"
        if endpoint:
            object_url = f"{endpoint}/{quote(storage['bucket'], safe='')}/{quote(name, safe='/')}"
    base = config.delivery["url_base"]
    # An R2 S3 API endpoint is not a browser delivery URL. Use a configured public
    # domain for that link, otherwise keep the note's local preview only.
    delivery = (
        f"{base}/{quote(relative, safe='/')}"
        if base
        else (None if config.provider == "r2" else object_url)
    )
    return object_url, delivery


def find_note(config: Config, record: Record) -> Path:
    expected = within(config.notes_root, record["note_path"])
    found = []
    for path in config.notes_root.rglob("*.md"):
        within(config.notes_root, path.relative_to(config.notes_root).as_posix())
        text = path.read_text(encoding="utf-8")
        if record["asset_id"] not in text and record["note_id"] not in text:
            continue
        data, _ = split_note(text)
        if data.get("assetId") == record["asset_id"] or data.get("noteId") == record["note_id"]:
            found.append(path)
    if len(found) > 1:
        raise ConflictError("Multiple proxy notes use the same asset/note ID.")
    return found[0] if found else expected


def render_note(
    config: Config,
    record: Record,
    path: Path,
    existing: str | None = None,
    *,
    sync_status: str | None = None,
) -> str:
    data, body = split_note(existing) if existing is not None else (CommentedMap(), "")
    if existing is not None and data.get("assetId") != record["asset_id"]:
        raise ConflictError(
            "Existing file is not this asset's generated note; choose a different name."
        )
    if data.get("assetId") not in {None, record["asset_id"]}:
        raise ConflictError("Proxy note belongs to a different asset.")
    version = data.get("schemaVersion")
    if version is not None and (
        not isinstance(version, str) or version not in {"1.0.0", "2.0.0", NOTE_SCHEMA_VERSION}
    ):
        raise AppError("Unsupported note schemaVersion; refusing to rewrite.")
    for old in ("blobName", "blobUrl"):
        if data.get("schemaVersion") == "1.0.0":
            data.pop(old, None)
    data.setdefault("type", "image")
    data.setdefault("title", Path(record["relative_path"]).name)
    data.setdefault("description", None)
    data.setdefault("tags", [])
    source = record.get("source")
    image = NoteStore(config).release(record)
    blob, url = urls(config, record["relative_path"])
    managed: dict[str, Any] = {
        "schemaVersion": NOTE_SCHEMA_VERSION,
        "conversionRecipe": record["release"].get("recipe"),
        "releaseGeneratedAt": record["release"].get("generated_at", record["updated_at"]),
        "sourceCapturedAt": source.get("captured_at", record["created_at"]) if source else None,
        "acquiredFrom": record["release"].get("acquired_from"),
        "assetId": record["asset_id"],
        "noteId": record["note_id"],
        "localPath": str(image),
        "releaseRef": "releases/" + record["relative_path"],
        "sourceAvailable": source is not None,
        "sourceRef": source["ref"] if source else None,
        "originalRef": source["path"] if source else None,
        "sourceSha256": source["sha256"] if source else None,
        "sha256": record["release"]["sha256"],
        "bytes": record["release"]["bytes"],
        "storageProvider": config.provider,
        "objectKey": "/".join(x for x in (config.storage["prefix"], record["relative_path"]) if x),
        "objectUrl": blob,
        "url": url,
        "cover": "releases/" + record["relative_path"],
    }
    if sync_status:
        managed["syncStatus"] = sync_status
    elif "syncStatus" not in data:
        managed["syncStatus"] = "local"
    for key, value in managed.items():
        if data.get(key) != value or key not in data:
            data[key] = value
    image_link = quote(os.path.relpath(image, path.parent).replace("\\", "/"), safe="/.")
    _, template_body = note_template()
    content = Template(template_body).substitute(
        title=str(data["title"]),
        image_link=image_link,
        local_url=image.as_uri(),
        remote_link=("\n\n[Remote image (access permissions apply)](" + url + ")") if url else "",
    )
    generated = content[content.index(BEGIN) : content.index(END) + len(END)]
    if LEGACY_BEGIN in body or LEGACY_END in body:
        if BEGIN in body or END in body:
            raise AppError("Mixed generated-note markers; repair them before refreshing.")
        if (
            body.count(LEGACY_BEGIN) != 1
            or body.count(LEGACY_END) != 1
            or body.index(LEGACY_BEGIN) > body.index(LEGACY_END)
        ):
            raise AppError("Malformed generated-note markers; repair them before refreshing.")
        body = (
            body[: body.index(LEGACY_BEGIN)]
            + generated
            + body[body.index(LEGACY_END) + len(LEGACY_END) :]
        )
    if not body.strip():
        body = content
    elif BEGIN in body or END in body:
        if body.count(BEGIN) != 1 or body.count(END) != 1 or body.index(BEGIN) > body.index(END):
            raise AppError("Malformed generated-note markers; repair them before refreshing.")
        body = body[: body.index(BEGIN)] + generated + body[body.index(END) + len(END) :]
    else:
        body = body.rstrip("\n") + "\n\n" + generated + "\n"
    # Note dates are independent of source/release lifecycle timestamps.
    if existing is None:
        stamp = now()
        data["created"] = stamp
        data["updated"] = stamp
    else:
        data.setdefault("created", record["created_at"])
        data.setdefault("updated", record["updated_at"])
    rendered = format_frontmatter(data) + "\n" + body.lstrip("\r\n")
    if existing is not None and rendered != existing:
        data["updated"] = now()
        rendered = format_frontmatter(data) + "\n" + body.lstrip("\r\n")
    return rendered


def format_existing_note(text: str) -> str:
    """Apply only the template layout; preserve values and the existing Markdown body."""
    data, body = split_note(text)
    if data.get("type") != "image" or not data.get("assetId"):
        raise AppError("Only image catalog notes can be formatted.")
    return format_frontmatter(data) + "\n" + body.lstrip("\r\n")


def refresh_note(
    config: Config,
    record: Record,
    *,
    dry_run: bool = False,
    sync_status: str | None = None,
) -> str:
    path = find_note(config, record)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    before = sha256(path) if path.exists() else None
    text = render_note(config, record, path, existing, sync_status=sync_status)
    record["note_path"] = path.relative_to(config.notes_root).as_posix()
    if existing == text:
        return "unchanged"
    if not dry_run:
        atomic_bytes(path, text.encode(), expected=before, create_only=existing is None)
        saved, _ = split_note(text)
        for field, key in (("created", "created_at"), ("updated", "updated_at")):
            value = saved[field]
            record[key] = value.isoformat() if isinstance(value, datetime) else value
    return "updated" if existing is not None else "created"


def refresh_notes(config: Config, selectors: list[str], *, dry_run: bool = False) -> list[Record]:
    store = NoteStore(config)
    result = []
    for record in store.select(selectors):
        store.check_release(record)
        status = refresh_note(config, record, dry_run=dry_run)
        result.append({"asset_id": record["asset_id"], "status": status})
    base = config.notes_root / "images.base"
    if not base.exists() and not dry_run:
        atomic_bytes(base, resource("images.base").encode())
    return result
