"""Conditional object transfers shared by AWS S3 and Cloudflare R2."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from contextlib import closing
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as SDKConfig
from botocore.exceptions import ClientError

from .assets import Record
from .config import Config
from .diagnostics import SERVICE_NAMES
from .errors import AppError, ConflictError
from .io import IMAGE_EXTENSIONS, safe_relative

# Single PUT keeps conditional writes identical on both services. No multipart fallback.
MAX_UPLOAD_BYTES = 5_000_000_000


class S3Objects:
    def __init__(self, config: Config):
        self.config = config
        settings = config.s3
        if not settings["bucket"]:
            raise AppError(
                f"{SERVICE_NAMES[config.provider]}: Set s3.bucket in the selected source before connecting."
            )
        if config.provider == "r2" and not settings["endpoint_url"]:
            raise AppError("Set s3.endpoint_url to the R2 account S3 API endpoint.")
        self.bucket = settings["bucket"]
        self.prefix = settings["prefix"] + "/" if settings["prefix"] else ""
        session = boto3.Session(profile_name=settings["profile"])
        self.client: Any = session.client(
            "s3",
            endpoint_url=settings["endpoint_url"],
            region_name="auto" if config.provider == "r2" else settings["region"],
            config=SDKConfig(
                signature_version="s3v4",
                connect_timeout=settings["timeout_seconds"],
                read_timeout=settings["timeout_seconds"],
                retries={"mode": "standard", "total_max_attempts": 3},
                s3={"addressing_style": "path"},
                # R2 supports Content-MD5; avoid optional AWS checksum trailers.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                ignore_configured_endpoint_urls=True,
            ),
        )

    def close(self) -> None:
        self.client.close()

    def name(self, relative: str) -> str:
        return self.prefix + safe_relative(relative)

    @staticmethod
    def properties(relative: str, item: dict[str, Any]) -> Record:
        if not isinstance(item.get("ETag"), str) or not item["ETag"]:
            raise ConflictError(
                "Object response has no ETag; conditional synchronization is required."
            )
        return {
            "relative_path": relative,
            "etag": item["ETag"],
            "bytes": item["ContentLength"],
            "metadata": dict(item.get("Metadata", {})),
            "version_id": item.get("VersionId"),
            "cache_control": item.get("CacheControl"),
        }

    def list(self) -> list[Record]:
        result = []
        seen: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if not key.startswith(self.prefix):
                    raise ConflictError("Storage returned an object outside the configured prefix.")
                relative = key[len(self.prefix) :]
                if Path(relative).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                safe_relative(relative)
                if relative.casefold() in seen:
                    raise ConflictError(
                        "Object names collide on Windows; no transfer was performed."
                    )
                seen.add(relative.casefold())
                # LIST has no user metadata/cache headers; inspect the listed revision.
                head = self.client.head_object(Bucket=self.bucket, Key=key, IfMatch=item["ETag"])
                if head.get("ETag") != item["ETag"]:
                    raise ConflictError("Object changed while listing the configured scope.")
                result.append(self.properties(relative, head))
        return sorted(result, key=lambda item: item["relative_path"])

    def get(self, relative: str) -> Record | None:
        try:
            item = self.client.head_object(Bucket=self.bucket, Key=self.name(relative))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return self.properties(relative, item)

    def _response(self, relative: str, etag: str) -> dict[str, Any]:
        response: dict[str, Any] = self.client.get_object(
            Bucket=self.bucket, Key=self.name(relative), IfMatch=etag
        )
        if response.get("ETag") != etag:
            response["Body"].close()
            raise ConflictError("Downloaded object does not match the inspected revision.")
        return response

    def digest(self, relative: str, etag: str) -> str:
        response = self._response(relative, etag)
        digest = hashlib.sha256()
        with closing(response["Body"]) as stream:
            for chunk in stream.iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def download(self, relative: str, etag: str) -> bytes:
        response = self._response(relative, etag)
        with closing(response["Body"]) as stream:
            content: bytes = stream.read()
        return content

    def validate_upload(self, source: Path) -> None:
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise AppError(
                f"{SERVICE_NAMES[self.config.provider]} uploads support at most "
                "5,000,000,000 bytes per image (single PUT)."
            )

    def upload(
        self, relative: str, source: Path, record: Record, previous: Record | None
    ) -> Record:
        self.validate_upload(source)
        size = source.stat().st_size
        metadata = dict(previous["metadata"]) if previous else {}
        metadata.update(asset_id=record["asset_id"], sha256=record["release"]["sha256"])
        cache = self.config.delivery["cache_control"]
        if cache is None and previous:
            cache = previous["cache_control"]
        options: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self.name(relative),
            "ContentLength": size,
            "ContentType": mimetypes.guess_type(relative)[0] or "application/octet-stream",
            "Metadata": metadata,
        }
        if cache is not None:
            options["CacheControl"] = cache
        if previous:
            options["IfMatch"] = previous["etag"]
        else:
            options["IfNoneMatch"] = "*"
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False))
            options["ContentMD5"] = base64.b64encode(digest.digest()).decode("ascii")
            stream.seek(0)
            uploaded = self.client.put_object(Body=stream, **options)
        etag = uploaded.get("ETag")
        if not etag:
            raise ConflictError("Upload returned no ETag; transfer was not marked synchronized.")
        head = self.client.head_object(Bucket=self.bucket, Key=self.name(relative), IfMatch=etag)
        remote = self.properties(relative, head)
        if remote["etag"] != etag or self.digest(relative, etag) != record["release"]["sha256"]:
            raise ConflictError("Uploaded bytes differ; transfer was not marked synchronized.")
        return remote

    def public_access(self) -> bool | None:
        # S3 bucket policy/ACLs cannot prove all delivery routes are private, and R2
        # public/custom-domain settings are outside the S3 API. Keep unknown explicit.
        return True if self.config.delivery["url_base"] else None
