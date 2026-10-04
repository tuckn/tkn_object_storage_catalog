from __future__ import annotations

import os
import re
from io import StringIO
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from .catalog import Catalog, Record
from .config import Config, resource
from .errors import AppError, ConflictError
from .io import atomic_bytes, sha256, within

BEGIN = "<!-- azure-blob-note:begin -->"
END = "<!-- azure-blob-note:end -->"


def split_note(text: str) -> tuple[CommentedMap, str]:
    yaml = YAML()
    yaml.preserve_quotes = True
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
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    stream = StringIO()
    yaml.dump(data, stream)
    return "---\n" + stream.getvalue() + "---\n" + body


def urls(config: Config, relative: str) -> tuple[str | None, str | None]:
    azure = config.azure
    blob = None
    if azure["account_url"] and azure["container"]:
        name = "/".join(part for part in (azure["prefix"], relative) if part)
        blob = (
            f"{azure['account_url']}/{quote(azure['container'], safe='$')}/{quote(name, safe='/')}"
        )
    base = config.delivery["url_base"]
    delivery = f"{base}/{quote(relative, safe='/')}" if base else blob
    return blob, delivery


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
    if data.get("schemaVersion") not in {None, "1.0.0"}:
        raise AppError("Unsupported note schemaVersion; refusing to rewrite.")
    data.setdefault("type", "image")
    data.setdefault("title", Path(record["relative_path"]).stem)
    data.setdefault("description", "")
    data.setdefault("category", Path(record["relative_path"]).parent.as_posix())
    for key in ("tags", "nouns", "domains", "projects"):
        data.setdefault(key, [])
    source = record.get("source")
    image = Catalog(config).release(record)
    blob, url = urls(config, record["relative_path"])
    managed: dict[str, Any] = {
        "schemaVersion": "1.0.0",
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
        "blobName": "/".join(x for x in (config.azure["prefix"], record["relative_path"]) if x),
        "blobUrl": blob,
        "url": url,
        "cover": (
            "releases/" + record["relative_path"]
            if config.notes_root.is_relative_to(config.data_root)
            else image.as_uri()
        ),
        "updated": record["updated_at"],
    }
    if sync_status:
        managed["syncStatus"] = sync_status
    elif "syncStatus" not in data:
        managed["syncStatus"] = "local"
    for key, value in managed.items():
        if data.get(key) != value or key not in data:
            data[key] = value
    if config.notes_root.is_relative_to(config.data_root):
        image_link = quote(os.path.relpath(image, path.parent).replace("\\", "/"), safe="/.")
    else:
        image_link = image.as_uri()
    content = Template(resource("note.md")).substitute(
        title=str(data["title"]),
        image_link=image_link,
        local_url=image.as_uri(),
        remote_link=("\n\n[Remote image (access permissions apply)](" + url + ")") if url else "",
    )
    generated = content[content.index(BEGIN) : content.index(END) + len(END)]
    if not body.strip():
        body = content
    elif BEGIN in body or END in body:
        if body.count(BEGIN) != 1 or body.count(END) != 1 or body.index(BEGIN) > body.index(END):
            raise AppError("Malformed generated-note markers; repair them before refreshing.")
        body = body[: body.index(BEGIN)] + generated + body[body.index(END) + len(END) :]
    else:
        body = body.rstrip("\n") + "\n\n" + generated + "\n"
    return serialize(data, body)


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
    return "updated" if existing is not None else "created"


def refresh_notes(config: Config, selectors: list[str], *, dry_run: bool = False) -> list[Record]:
    catalog = Catalog(config)
    result = []
    for record in catalog.select(selectors):
        catalog.check_release(record)
        status = refresh_note(config, record, dry_run=dry_run)
        if not dry_run:
            catalog.save(record)
        result.append({"asset_id": record["asset_id"], "status": status})
    base = config.notes_root / "images.base"
    if not base.exists() and not dry_run:
        atomic_bytes(base, resource("images.base").encode())
    return result
