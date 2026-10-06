from __future__ import annotations

import re
from copy import copy
from datetime import date
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import RoundTripConstructor
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# Recognize complete date values, not dates embedded in prose, paths or URLs.
ISO_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
)


class NoteConstructor(RoundTripConstructor):
    def timestamp_text(self, node: ScalarNode) -> str:
        # Retain the original offset, Z and fractional precision before parsing.
        return str(node.value)


NoteConstructor.add_constructor("tag:yaml.org,2002:timestamp", NoteConstructor.timestamp_text)


def note_yaml() -> YAML:
    yaml = YAML()
    yaml.Constructor = NoteConstructor
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def quote_dates(value: Any) -> Any:
    """Copy containers without changing comments; render date values as strings."""
    if isinstance(value, date):
        return DoubleQuotedScalarString(value.isoformat())
    if isinstance(value, str) and ISO_DATE.fullmatch(value):
        return DoubleQuotedScalarString(value)
    if isinstance(value, dict):
        result = copy(value)
        for key, item in value.items():
            result[key] = quote_dates(item)
        return result
    if isinstance(value, list):
        sequence = copy(value)
        for index, item in enumerate(value):
            sequence[index] = quote_dates(item)
        return sequence
    return value
