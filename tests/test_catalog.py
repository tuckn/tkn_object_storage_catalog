from __future__ import annotations

import json

import pytest

from tkn_azure_blob_note.catalog import Catalog
from tkn_azure_blob_note.errors import AppError


@pytest.mark.parametrize(
    "field,value",
    [
        ("asset_id", 1),
        ("relative_path", None),
        ("release", {}),
        ("source", {"path": "originals/a"}),
    ],
)
def test_malformed_catalog_is_actionable(cfg, asset, field, value):
    path = cfg.data_root / "catalog" / (asset["asset_id"] + ".json")
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(AppError, match="Invalid catalog"):
        Catalog(cfg).assets()
