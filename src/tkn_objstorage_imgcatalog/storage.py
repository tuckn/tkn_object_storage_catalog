"""Cloud-independent storage contract and adapter selection."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .assets import Record
from .config import Config


class ObjectStore(Protocol):
    def list(self) -> list[Record]: ...
    def get(self, relative: str) -> Record | None: ...
    def digest(self, relative: str, etag: str) -> str: ...
    def download(self, relative: str, etag: str) -> bytes: ...
    def validate_upload(self, source: Path) -> None: ...
    def upload(
        self, relative: str, source: Path, record: Record, previous: Record | None
    ) -> Record: ...
    def public_access(self) -> bool | None: ...
    def close(self) -> None: ...


def open_store(config: Config) -> ObjectStore:
    if config.provider == "azure":
        from .azure import AzureBlobs

        return AzureBlobs(config)
    from .s3 import S3Objects

    return S3Objects(config)
