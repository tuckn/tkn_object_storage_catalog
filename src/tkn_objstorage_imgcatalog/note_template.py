from __future__ import annotations

import re
from copy import deepcopy
from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString
from ruamel.yaml.tokens import CommentToken

from .config import resource
from .errors import AppError
from .yaml_dates import quote_dates

# Section comments with this syntax belong to the template, not the user's values.
SECTION = re.compile(r"(?m)^[ \t]*# --- .+ ---[ \t]*(?:\r?\n|$)")


def note_template() -> tuple[list[tuple[str, list[str]]], str]:
    """Read the field order, section comments and body from the Markdown resource."""
    text = resource("note.md")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        raise AppError("note.md template requires YAML Frontmatter and a Markdown body.")
    header = match.group(1)
    try:
        fields = YAML(typ="safe").load(header)
    except YAMLError as exc:
        raise AppError("Invalid or duplicate Frontmatter keys in note.md template.") from exc
    if not isinstance(fields, dict) or "tags" not in fields:
        raise AppError("note.md template must declare flat fields including tags.")
    groups = []
    seen = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", header):
        comments, keys = [], []
        for line in block.splitlines():
            if not line.strip():
                continue
            if SECTION.fullmatch(line + "\n"):
                comments.append(line)
                continue
            field = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):[ \t]*", line)
            if not field:
                raise AppError(
                    "note.md fields must be flat 'key:' declarations; "
                    "section comments must use '# --- English label ---'."
                )
            keys.append(field.group(1))
        seen.extend(keys)
        groups.append(("\n".join(comments), keys))
    if seen != list(fields):
        raise AppError("Invalid field declarations in note.md template.")
    return groups, text[match.end() :].lstrip("\r\n")


def clean_comments(value: Any) -> Any:
    """Remove prior template sections while retaining user comments."""
    if isinstance(value, CommentToken):
        value.value = SECTION.sub("", value.value).rstrip() + "\n"
        return value if value.value.strip() else None
    if isinstance(value, list):
        return [clean_comments(item) for item in value]
    return value


def format_frontmatter(data: CommentedMap) -> str:
    groups, _ = note_template()
    # ruamel TimeStamp.__deepcopy__ can discard timezone information.
    # Values are only replaced here; copy comment metadata independently.
    values = quote_dates(CommentedMap(data))
    values.ca.items.update(deepcopy(data.ca.items))
    values.ca.comment = deepcopy(data.ca.comment)
    values.pop("category", None)
    for key in ("nouns", "domains", "projects"):
        if key in values and values[key] == []:
            del values[key]
    if values.get("description") == "":
        values["description"] = None
    for key, value in values.items():
        if isinstance(value, dict) or (
            isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value)
        ):
            raise AppError(
                "Image Frontmatter must be flat; nested properties were retained on disk."
            )
        if isinstance(value, str):
            if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
                values[key] = SingleQuotedScalarString(value)
            elif key in {"schemaVersion", "title", "cover"}:
                values[key] = DoubleQuotedScalarString(value)
    declared = {key for _, keys in groups for key in keys}
    extras = [key for key in values if key not in declared]
    expanded = []
    for comments, keys in groups:
        if "tags" in keys and extras:
            # Keep manually added properties before the common management footer.
            before, after = keys[: keys.index("tags")], keys[keys.index("tags") :]
            expanded.append((comments, before))
            expanded.append(("", extras))
            expanded.append(("", after))
        else:
            expanded.append((comments, keys))
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    chunks: list[str] = []
    for comments, keys in expanded:
        present = [key for key in keys if key in values]
        if not present:
            continue
        section = CommentedMap((key, values[key]) for key in present)
        for key in present:
            if key in values.ca.items:
                section.ca.items[key] = clean_comments(deepcopy(values.ca.items[key]))
        if not chunks and values.ca.comment:
            section.ca.comment = clean_comments(deepcopy(values.ca.comment))
        stream = StringIO()
        yaml.dump(section, stream)
        chunks.append((comments + "\n" if comments else "") + stream.getvalue().strip("\n"))
    return "---\n" + "\n\n".join(chunks) + "\n---\n"
