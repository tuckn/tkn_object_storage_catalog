from __future__ import annotations

import hashlib
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, features
from PIL import __version__ as pillow_version

from .catalog import Catalog, Operation, Record, make_record
from .config import Config
from .errors import AppError, ConflictError
from .io import atomic_bytes, copy_verified, fingerprint, image_files, now, sha256, within
from .notes import refresh_note

CONVERTIBLE = {".jpg", ".jpeg", ".png"}


def should_convert(path: Path, config: Config) -> bool:
    if not config.conversion["enabled"] or path.suffix.lower() not in CONVERTIBLE:
        return False
    try:
        with Image.open(path) as image:
            if getattr(image, "n_frames", 1) != 1:
                return False
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AppError(f"Cannot validate image for conversion: {path.name}") from exc
    if not features.check("webp"):
        raise AppError("This Pillow installation cannot encode WebP.")
    return True


def recipe(config: Config) -> str:
    return fingerprint(
        {
            "conversion": config.conversion,
            "pillow": pillow_version,
            "webp": features.version("webp"),
        }
    )


def render_image(source: Path, config: Config, convert: bool) -> bytes:
    if not convert:
        return source.read_bytes()
    try:
        with Image.open(source) as image:
            normalized = ImageOps.exif_transpose(image).convert(
                "RGBA"
                if image.mode in {"RGBA", "LA", "P"} or "transparency" in image.info
                else "RGB"
            )
            options: dict[str, Any] = {
                "quality": config.conversion["quality"],
                "lossless": config.conversion["lossless"],
                "method": 6,
            }
            if not config.conversion["strip_metadata"]:
                options.update(
                    {
                        key: normalized.info[key]
                        for key in ("exif", "icc_profile", "xmp")
                        if key in normalized.info
                    }
                )
            result = BytesIO()
            normalized.save(result, format="WEBP", **options)
            payload = result.getvalue()
        with Image.open(BytesIO(payload)) as check:
            check.verify()
        return payload
    except (OSError, ValueError) as exc:
        raise AppError(f"Image conversion failed: {source.name}") from exc


def import_images(
    config: Config, inputs: list[Path], operation: Operation, *, name: str | None = None
) -> list[Record]:
    catalog = Catalog(config)
    inputs = inputs or [config.data_root / "staging"]
    candidates: list[tuple[Path, str]] = []
    for supplied in inputs:
        supplied = supplied.expanduser().resolve()
        if not supplied.exists() and supplied == config.data_root / "staging":
            continue
        for source in image_files(supplied):
            relative = (
                source.name if supplied.is_file() else source.relative_to(supplied).as_posix()
            )
            candidates.append((source, relative))
    if name and len(candidates) != 1:
        raise AppError("--name requires exactly one image.")
    existing = {r["relative_path"].casefold(): r for r in catalog.assets()}
    planned: list[tuple[Path, str, str, bool, Record | None]] = []
    seen: set[str] = set()
    for source, relative in candidates:
        if source.is_relative_to(config.data_root) and not source.is_relative_to(
            config.data_root / "staging"
        ):
            raise AppError("import only accepts external inputs or data/staging.")
        relative = name or relative
        convert = should_convert(source, config)
        if convert:
            relative = str(Path(relative).with_suffix(".webp")).replace("\\", "/")
        if not convert and Path(relative).suffix.lower() != source.suffix.lower():
            raise AppError(
                "--name must keep the input extension when conversion is disabled or skipped."
            )
        target = within(config.data_root / "releases", relative)
        if relative.casefold() in seen:
            raise ConflictError("Inputs map to the same release name; use distinct --name values.")
        seen.add(relative.casefold())
        digest = sha256(source)
        old = existing.get(relative.casefold())
        if old:
            catalog.check_release(old)
            previous = old.get("source")
            if not previous or previous["sha256"] != digest:
                raise ConflictError(f"An asset already uses {relative}; choose another --name.")
            if old["relative_path"] != relative:
                raise ConflictError("Name differs only in case; use its existing spelling.")
            if old["release"].get("recipe") != recipe(config):
                raise ConflictError("Conversion settings changed; run build for this asset.")
        elif target.exists():
            raise ConflictError(f"Unmanaged release already exists: {relative}")
        if old:
            refresh_note(config, old, dry_run=True)
        elif within(config.notes_root, relative + ".md").exists():
            raise ConflictError("A note already uses the intended path; choose another --name.")
        planned.append((source, relative, digest, convert, old))
    result = []
    for source, relative, digest, convert, old in planned:
        if old:
            if not operation.dry_run:
                refresh_note(config, old)
                catalog.save(old)
            result.append({"asset_id": old["asset_id"], "status": "unchanged", "path": relative})
            continue
        status = {
            "status": "created",
            "path": relative,
            "source_sha256": digest,
            "conversion": "webp" if convert else "copy",
        }
        if operation.dry_run:
            result.append(status)
            continue
        original_rel = "originals/" + digest + "/" + source.name
        original = within(config.data_root, original_rel)
        operation.event(
            "capture_started",
            source_ref=source.as_uri(),
            source_sha256=digest,
            original_ref=original_rel,
            relative_path=relative,
        )
        copy_verified(source, original, digest)
        content = render_image(original, config, convert)
        release_digest = hashlib.sha256(content).hexdigest()
        record = make_record(
            relative,
            source.as_uri() + "|" + relative,
            source={
                "ref": source.as_uri(),
                "path": original_rel,
                "sha256": digest,
                "captured_at": now(),
                "entity_id": "urn:sha256:" + digest,
            },
            digest=release_digest,
            size=len(content),
            recipe=recipe(config),
        )
        operation.event("release_prepared", record=record)
        atomic_bytes(catalog.release(record), content, create_only=True)
        catalog.save(record)
        refresh_note(config, record)
        catalog.save(record)
        operation.event(
            "imported",
            asset_id=record["asset_id"],
            used="urn:sha256:" + digest,
            generated=record["release"]["entity_id"],
        )
        result.append({**status, "asset_id": record["asset_id"]})
    return result


def build_images(config: Config, selectors: list[str], operation: Operation) -> list[Record]:
    catalog = Catalog(config)
    result = []
    for record in catalog.select(selectors):
        catalog.check_release(record)
        refresh_note(config, record, dry_run=True)
        source = record.get("source")
        if not source:
            result.append(
                {
                    "asset_id": record["asset_id"],
                    "status": "skipped",
                    "reason": "original_unavailable",
                }
            )
            continue
        original = within(config.data_root, source["path"])
        if not original.is_file() or sha256(original) != source["sha256"]:
            raise AppError("Original image is missing or changed; refusing to build.")
        if record["release"].get("recipe") == recipe(config):
            result.append({"asset_id": record["asset_id"], "status": "unchanged"})
            continue
        convert = should_convert(original, config)
        expected_suffix = ".webp" if convert else original.suffix
        if Path(record["relative_path"]).suffix.lower() != expected_suffix.lower():
            raise ConflictError(
                "Changing format would rename the object; import under a new --name."
            )
        if not operation.dry_run:
            content = render_image(original, config, convert)
            updated = deepcopy(record)
            updated["release"].update(
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
                recipe=recipe(config),
                generated_at=now(),
            )
            updated["release"]["entity_id"] = "urn:sha256:" + updated["release"]["sha256"]
            updated["updated_at"] = now()
            operation.event("release_prepared", record=updated)
            catalog.archive_release(record)
            atomic_bytes(catalog.release(record), content, expected=record["release"]["sha256"])
            catalog.save(updated)
            refresh_note(config, updated, sync_status="local")
            catalog.save(updated)
            operation.event(
                "built",
                asset_id=record["asset_id"],
                used=source["entity_id"],
                generated=updated["release"]["entity_id"],
            )
        result.append({"asset_id": record["asset_id"], "status": "updated"})
    return result
