from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from .catalog import Record
from .config import Config
from .errors import AppError, ConflictError
from .io import IMAGE_EXTENSIONS, safe_relative


class AzureBlobs:
    def __init__(self, config: Config):
        self.config = config
        a = config.azure
        if not a["account_url"] or not a["container"]:
            raise AppError(
                "Set azure.account_url and azure.container with config init, then config list."
            )
        if a["auth"] == "azure_cli":
            self.credential: Any = AzureCliCredential(process_timeout=a["timeout_seconds"])
        else:
            self.credential = ManagedIdentityCredential(client_id=a["managed_identity_client_id"])
        self.client = BlobServiceClient(
            a["account_url"],
            credential=self.credential,
            retry_total=2,
            connection_timeout=a["timeout_seconds"],
            read_timeout=a["timeout_seconds"],
        )
        self.container = self.client.get_container_client(a["container"])
        self.prefix = a["prefix"] + "/" if a["prefix"] else ""
        self.timeout = a["timeout_seconds"]

    def close(self) -> None:
        self.client.close()
        self.credential.close()

    def name(self, relative: str) -> str:
        return self.prefix + safe_relative(relative)

    @staticmethod
    def properties(relative: str, item: Any) -> Record:
        settings = item.content_settings
        return {
            "relative_path": relative,
            "etag": item.etag,
            "bytes": item.size,
            "metadata": dict(item.metadata or {}),
            "version_id": item.version_id,
            "cache_control": settings.cache_control,
        }

    def list(self) -> list[Record]:
        result = []
        seen: set[str] = set()
        for item in self.container.list_blobs(
            name_starts_with=self.prefix, include=["metadata"], timeout=self.timeout
        ):
            relative = item.name[len(self.prefix) :]
            if Path(relative).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            safe_relative(relative)
            if relative.casefold() in seen:
                raise ConflictError("Blob names collide on Windows; no files were downloaded.")
            seen.add(relative.casefold())
            result.append(self.properties(relative, item))
        return sorted(result, key=lambda r: r["relative_path"])

    def get(self, relative: str) -> Record | None:
        try:
            item = self.container.get_blob_client(self.name(relative)).get_blob_properties(
                timeout=self.timeout
            )
        except ResourceNotFoundError:
            return None
        return self.properties(relative, item)

    def digest(self, relative: str, etag: str) -> str:
        response = self.container.get_blob_client(self.name(relative)).download_blob(
            etag=etag, match_condition=MatchConditions.IfNotModified, timeout=self.timeout
        )
        digest = hashlib.sha256()
        for chunk in response.chunks():
            digest.update(chunk)
        return digest.hexdigest()

    def download(self, relative: str, etag: str) -> bytes:
        return (
            self.container.get_blob_client(self.name(relative))
            .download_blob(
                etag=etag, match_condition=MatchConditions.IfNotModified, timeout=self.timeout
            )
            .readall()
        )

    def validate_upload(self, source: Path) -> None:
        """Azure SDK handles chunked transfers; no additional local size limit."""

    def upload(
        self, relative: str, source: Path, record: Record, previous: Record | None
    ) -> Record:
        metadata = dict(previous["metadata"]) if previous else {}
        metadata.update(asset_id=record["asset_id"], sha256=record["release"]["sha256"])
        media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        cache = self.config.delivery["cache_control"]
        if cache is None and previous:
            cache = previous["cache_control"]
        options: dict[str, Any] = {
            "overwrite": previous is not None,
            "metadata": metadata,
            "content_settings": ContentSettings(content_type=media_type, cache_control=cache),
            "timeout": self.timeout,
            "validate_content": True,
        }
        if previous:
            options.update(etag=previous["etag"], match_condition=MatchConditions.IfNotModified)
        with source.open("rb") as stream:
            self.container.get_blob_client(self.name(relative)).upload_blob(stream, **options)
        remote = self.get(relative)
        if remote is None:
            raise ConflictError("Uploaded blob disappeared; transfer was not marked synchronized.")
        if self.digest(relative, remote["etag"]) != record["release"]["sha256"]:
            raise ConflictError("Uploaded bytes differ; transfer was not marked synchronized.")
        return remote

    def public_access(self) -> bool | None:
        if self.config.azure["container"] == "$web" or self.config.delivery["url_base"]:
            return True
        try:
            policy = self.container.get_container_access_policy(timeout=self.timeout)
            return policy["public_access"] is not None
        except HttpResponseError:
            # A data-plane identity may lack permission to read container ACLs.
            return None
