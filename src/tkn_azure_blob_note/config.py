from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .errors import AppError, ConflictError
from .io import SCHEMA_VERSION, atomic_bytes, check_schema, now, safe_relative, sha256

APPLICATION_ID = "azure_blob_note"


def resource(name: str) -> str:
    return files("tkn_azure_blob_note").joinpath("resources", name).read_text(encoding="utf-8")


def user_root() -> Path:
    return Path.home() / ".tkn" / APPLICATION_ID


def flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    output = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            output.update(flatten(item, name))
        else:
            output[name] = item
    return output


def parse_yaml(text: str, label: str) -> dict[str, Any]:
    try:
        result = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise AppError(f"{label}: invalid or duplicate YAML keys.") from exc
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise AppError(f"{label}: expected a YAML mapping.")
    return result


def validate_part(value: dict[str, Any], defaults: dict[str, Any], label: str) -> None:
    check_schema(value, label)
    allowed = flatten(defaults)
    for key, item in value.items():
        if key not in defaults:
            raise AppError(f"{label}: unknown setting {key}.")
        if isinstance(defaults[key], dict) and not isinstance(item, dict):
            raise AppError(f"{label}: {key} must be a mapping.")
    for key, item in flatten(value).items():
        if key not in allowed:
            raise AppError(f"{label}: unknown setting {key}.")
        default = allowed[key]
        if default is not None and type(item) is not type(default):
            raise AppError(f"{label}: wrong type for {key}.")
        if default is None and item is not None and not isinstance(item, str):
            raise AppError(f"{label}: {key} must be a string or null.")
        if key.endswith("_root") and item == "":
            raise AppError(f"{label}: {key} must not be empty.")
    if "conversion" in value:
        c = value["conversion"]
        if c.get("format", "webp") != "webp":
            raise AppError(f"{label}: conversion.format only supports webp.")
        if not 0 <= c.get("quality", 82) <= 100:
            raise AppError(f"{label}: quality must be between 0 and 100.")
    if "azure" in value:
        a = value["azure"]
        if a.get("auth", "azure_cli") not in {"azure_cli", "managed_identity"}:
            raise AppError(f"{label}: azure.auth must be azure_cli or managed_identity.")
        if a.get("timeout_seconds", 60) <= 0:
            raise AppError(f"{label}: timeout_seconds must be positive.")
        if a.get("prefix"):
            safe_relative(a["prefix"].rstrip("/"))
        container = a.get("container")
        if (
            container is not None
            and container != "$web"
            and not re.fullmatch(r"[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]", container)
        ):
            raise AppError(f"{label}: invalid Azure container name.")
    for key in ("azure.account_url", "delivery.url_base"):
        item = flatten(value).get(key)
        if item is not None:
            url = urlsplit(item)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise AppError(f"{label}: {key} must be HTTPS without credentials/query/fragment.")
            if key == "azure.account_url" and url.path not in {"", "/"}:
                raise AppError(f"{label}: account_url must not include a container or path.")


def merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "schema_version":
            continue
        if isinstance(value, dict):
            merge(target[key], value)
        else:
            target[key] = value


@dataclass(frozen=True)
class Config:
    values: dict[str, Any]
    sources: list[dict[str, Any]]
    origins: dict[str, str]

    @property
    def data_root(self) -> Path:
        return Path(self.values["data_root"])

    @property
    def state_root(self) -> Path:
        return Path(self.values["state_root"])

    @property
    def notes_root(self) -> Path:
        return Path(self.values["notes_root"])

    @property
    def azure(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.values["azure"])

    @property
    def conversion(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.values["conversion"])

    @property
    def delivery(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.values["delivery"])

    def report(self) -> dict[str, Any]:
        return {
            "config": self.values,
            "sources": self.sources,
            "origins": self.origins,
            "effective_schema_version": SCHEMA_VERSION,
        }


def load_config(
    explicit: Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    home = home or user_root()
    defaults = parse_yaml(resource("config.example.yaml"), "built-in")
    validate_part(defaults, defaults, "built-in")
    values = deepcopy(defaults)
    origins = {key: "built-in" for key in flatten(defaults) if key != "schema_version"}
    sources = [{"source": "built-in", "schema_version": SCHEMA_VERSION}]
    candidates = [(home / "config.yaml", False), (cwd / ".tkn" / "config.yaml", False)]
    if explicit is not None:
        candidates.append((explicit.expanduser().resolve(), True))
    for path, required in candidates:
        if not path.exists():
            if required:
                raise AppError(f"Explicit config not found: {path}")
            continue
        value = parse_yaml(path.read_text(encoding="utf-8-sig"), str(path))
        validate_part(value, defaults, str(path))
        merge(values, value)
        origins.update({key: str(path) for key in flatten(value) if key != "schema_version"})
        sources.append({"source": str(path), "schema_version": value["schema_version"]})
    if overrides:
        partial = {"schema_version": SCHEMA_VERSION, **overrides}
        validate_part(partial, defaults, "CLI")
        merge(values, partial)
        origins.update({key: "CLI" for key in flatten(overrides)})
    for key in ("data_root", "state_root", "notes_root"):
        raw = values[key]
        if raw is None:
            raw = str(Path(values["data_root"]) / "notes")
        path = Path(raw).expanduser()
        values[key] = str((path if path.is_absolute() else cwd / path).resolve())
    values["azure"]["prefix"] = values["azure"]["prefix"].rstrip("/")
    for group, key in (("azure", "account_url"), ("delivery", "url_base")):
        if values[group][key]:
            values[group][key] = values[group][key].rstrip("/")
    data, state, notes = (Path(values[k]) for k in ("data_root", "state_root", "notes_root"))
    if data == state or data.is_relative_to(state) or state.is_relative_to(data):
        raise AppError("data_root and state_root must be separate directory trees.")
    for reserved in (
        "staging",
        "originals",
        "releases",
        "catalog",
        "provenance",
        "history",
    ):
        root = data / reserved
        if notes == root or notes.is_relative_to(root) or root.is_relative_to(notes):
            raise AppError("notes_root must not overlap the managed image/catalog directories.")
    if notes == state or notes.is_relative_to(state) or state.is_relative_to(notes):
        raise AppError("notes_root must not overlap state_root.")
    return Config(values, sources, origins)


def init_config(path: Path, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    content = resource("config.example.yaml").encode()
    status = "created"
    backup = None
    previous = path.read_bytes() if path.exists() else None
    expected = sha256(path) if previous is not None else None
    if previous is not None:
        if path.read_bytes() == content:
            return {"status": "unchanged", "path": str(path), "dry_run": dry_run}
        if not force:
            raise ConflictError(
                "Config already exists and differs; use --force to back it up and replace."
            )
        status = "replaced"
        backup = path.with_name(path.name + "." + now().replace(":", "-") + ".bak")
    if not dry_run:
        if backup:
            assert previous is not None
            atomic_bytes(backup, previous, create_only=True)
        atomic_bytes(path, content, expected=expected, create_only=previous is None)
    return {
        "status": status,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "dry_run": dry_run,
    }
