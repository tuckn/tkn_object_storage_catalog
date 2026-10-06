from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from tkn_objstorage_imgcatalog.catalog import Catalog, Operation
from tkn_objstorage_imgcatalog.config import load_config
from tkn_objstorage_imgcatalog.errors import ConflictError
from tkn_objstorage_imgcatalog.images import import_images


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return load_config(
        home=tmp_path / "settings",
        cwd=tmp_path,
        overrides={
            "data_root": str(tmp_path / "data"),
            "state_root": str(tmp_path / "state"),
            "azure": {
                "account_url": "https://example.blob.core.windows.net",
                "container": "images",
            },
        },
    )


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "inputs" / "example.png"
    path.parent.mkdir()
    Image.new("RGB", (20, 16), (12, 100, 210)).save(path)
    return path


@pytest.fixture
def asset(cfg, source):
    with Operation(cfg, "import", False) as operation:
        import_images(cfg, [source], operation)
    return Catalog(cfg).assets()[0]


class FakeBlobs:
    def __init__(self):
        self.values = {}
        self.serial = 0
        self.writes = 0
        self.public = False
        self.race = False

    def put(self, relative, payload):
        self.serial += 1
        self.values[relative] = (payload, str(self.serial))

    def get(self, relative):
        if relative not in self.values:
            return None
        payload, etag = self.values[relative]
        return {
            "relative_path": relative,
            "etag": etag,
            "bytes": len(payload),
            "metadata": {},
            "version_id": None,
            "cache_control": None,
        }

    def list(self):
        return [self.get(name) for name in sorted(self.values)]

    def download(self, relative, etag):
        payload, current = self.values[relative]
        if etag != current:
            raise ConflictError("Remote changed.")
        return payload

    def digest(self, relative, etag):
        return hashlib.sha256(self.download(relative, etag)).hexdigest()

    def validate_upload(self, source):
        pass

    def upload(self, relative, source, record, previous):
        current = self.get(relative)
        if self.race or (current and (not previous or current["etag"] != previous["etag"])):
            raise ConflictError("Remote changed before conditional upload.")
        self.put(relative, source.read_bytes())
        self.writes += 1
        return self.get(relative)

    def close(self):
        pass

    def public_access(self):
        return self.public


@pytest.fixture
def blobs():
    return FakeBlobs()
