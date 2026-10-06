from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .errors import AppError, ConflictError
from .io import atomic_bytes, check_schema, now, safe_relative, sha256

APPLICATION_ID = "objstorage-imgcatalog"
CONFIG_SCHEMA_VERSION = "3.1.0"
DEFAULT_SOURCE_ID = "my-obj-storage-1"
LEGACY_SOURCE_ID = "images"


def resource(name: str) -> str:
    return (
        files("tkn_objstorage_imgcatalog").joinpath("resources", name).read_text(encoding="utf-8")
    )


def user_root() -> Path:
    return Path.home() / ".tkn" / APPLICATION_ID


def legacy_root() -> Path:
    return Path.home() / ".tkn" / "azure_blob_note"


def flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    output = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
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
    for key, item in value.items():
        if key not in defaults:
            raise AppError(f"{label}: unknown setting {key}.")
        default = defaults[key]
        if isinstance(default, dict):
            if not isinstance(item, dict):
                raise AppError(f"{label}: {key} must be a mapping.")
            validate_part(item, default, f"{label}.{key}")
        elif default is not None and type(item) is not type(default):
            raise AppError(f"{label}: wrong type for {key}.")
        elif default is None and item is not None and not isinstance(item, str):
            raise AppError(f"{label}: {key} must be a string or null.")
        if isinstance(key, str) and key.endswith("_root") and item == "":
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
    if "provider" in value and value["provider"] not in {"azure", "s3", "r2"}:
        raise AppError(f"{label}: provider must be azure, s3, or r2.")
    if "s3" in value:
        s = value["s3"]
        if s.get("timeout_seconds", 60) <= 0:
            raise AppError(f"{label}: timeout_seconds must be positive.")
        if s.get("prefix"):
            safe_relative(s["prefix"].rstrip("/"))
        bucket = s.get("bucket")
        if bucket is not None and (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or ".." in bucket
            or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket)
        ):
            raise AppError(f"{label}: invalid S3/R2 bucket name.")
        for key in ("profile", "region"):
            if s.get(key) is not None and not s[key].strip():
                raise AppError(f"{label}: s3.{key} must not be empty.")
        if s.get("region") and not re.fullmatch(r"[a-z0-9-]+", s["region"]):
            raise AppError(f"{label}: invalid s3.region.")
    for key in ("azure.account_url", "s3.endpoint_url", "delivery.url_base"):
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
            if key != "delivery.url_base" and url.path not in {"", "/"}:
                raise AppError(f"{label}: {key} must not include a container, bucket, or path.")
    validate_provider(value, label)


def validate_provider(value: dict[str, Any], label: str) -> None:
    provider = value.get("provider", "azure")
    settings = value.get("s3", {})
    if provider == "r2":
        endpoint = settings.get("endpoint_url")
        host = urlsplit(endpoint).hostname if endpoint else None
        if host and not re.fullmatch(
            r"[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com", host
        ):
            raise AppError(f"{label}: r2 requires its account S3 API endpoint.")
        if settings.get("region") not in {None, "auto"}:
            raise AppError(f"{label}: r2 region must be auto.")
    elif provider == "s3" and settings.get("region") == "auto":
        raise AppError(f"{label}: auto is an R2 region, not an AWS region.")


def merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict):
            merge(target[key], value)
        else:
            target[key] = value


def validate_source_id(source_id: Any, label: str) -> None:
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source_id):
        raise AppError(f"{label}: source IDs must use 1-64 lowercase letters, digits, '_' or '-'.")
    safe_relative(source_id)


def validate_integration_tests(value: Any, label: str) -> None:
    """Validate non-secret test contracts without registering ordinary sources."""
    if not isinstance(value, dict):
        raise AppError(f"{label}: integration_tests must be a mapping.")
    fields = {"provider", "endpoint_url", "bucket", "cleanup", "expected_target_sha256"}
    for name, target in value.items():
        validate_source_id(name, label)
        expected_fields = fields | (
            {"region", "profile", "account_id", "role_arn"}
            if isinstance(target, dict) and target.get("provider") == "s3"
            else set()
        )
        if not isinstance(target, dict) or set(target) != expected_fields:
            raise AppError(f"{label}.{name}: expected only {', '.join(sorted(expected_fields))}.")
        if any(not isinstance(item, str) for key, item in target.items() if key != "endpoint_url"):
            raise AppError(f"{label}.{name}: all test settings must be strings.")
        provider = target["provider"]
        if provider not in {"azure", "r2", "s3"}:
            raise AppError(f"{label}.{name}: provider must be azure, r2, or s3.")
        if target["cleanup"] != ("retain" if provider == "azure" else "manifest"):
            raise AppError(f"{label}.{name}: Azure must retain; R2/S3 must use manifest cleanup.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", target["bucket"]) or not (
            {"test", "tests"} & set(target["bucket"].split("-"))
        ):
            raise AppError(f"{label}.{name}: bucket/container must be a dedicated test name.")
        endpoint_pattern = (
            r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net"
            if provider == "azure"
            else r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com"
        )
        if provider == "s3":
            if (
                target["endpoint_url"] is not None
                or not re.fullmatch(r"(?:us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d+", target["region"])
                or not re.fullmatch(r"[a-zA-Z0-9_-]*tests?[a-zA-Z0-9_-]*", target["profile"])
                or not re.fullmatch(r"\d{12}", target["account_id"])
                or not re.fullmatch(
                    rf"arn:aws:iam::{target['account_id']}:role/[\w+=,.@-]+",
                    target["role_arn"],
                )
            ):
                raise AppError(
                    f"{label}.{name}: invalid S3 standard endpoint, region or test identity."
                )
        elif not isinstance(target["endpoint_url"], str) or not re.fullmatch(
            endpoint_pattern, target["endpoint_url"]
        ):
            raise AppError(
                f"{label}.{name}: expected a service endpoint without credentials or paths."
            )
        if not re.fullmatch(r"[0-9a-f]{64}", target["expected_target_sha256"]):
            raise AppError(f"{label}.{name}: expected_target_sha256 must be a reviewed SHA-256.")


def normalize_layer(
    value: dict[str, Any], defaults: dict[str, Any], label: str
) -> tuple[dict[str, Any], bool]:
    version = value.get("schema_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise AppError(f"{label}: schema_version must be a quoted MAJOR.MINOR.PATCH string.")
    major, minor, _ = map(int, version.split("."))
    if (major, minor) == (1, 0):
        check_schema(value, label)
        legacy_defaults = {
            "schema_version": "1.0.0",
            **defaults,
            "data_root": str(legacy_root() / "data"),
            "state_root": str(legacy_root() / "state"),
        }
        validate_part(value, legacy_defaults, label)
        # Preserve existing storage locations; loading never moves data or rewrites YAML.
        legacy = {key: item for key, item in value.items() if key != "schema_version"}
        return {"sources": {LEGACY_SOURCE_ID: legacy}}, True
    if (major, minor) not in {(2, 0), (3, 0), (3, 1)}:
        raise AppError(
            f"{label}: unsupported config schema {version}; supported schema is 3.1.x (also reads 1.0.x/2.0.x/3.0.x)."
        )
    layer: dict[str, Any] = {}
    allowed = {"schema_version", "sources"}
    if (major, minor) == (3, 1):
        allowed.add("integration_tests")
    if "integration_tests" in value and "integration_tests" not in allowed:
        raise AppError(f"{label}: integration_tests requires config schema 3.1.0.")
    for key in value:
        if key not in allowed:
            raise AppError(f"{label}: move {key} into sources.<source-id>.{key}.")
    if "integration_tests" in value:
        validate_integration_tests(value["integration_tests"], f"{label}.integration_tests")
        layer["integration_tests"] = deepcopy(value["integration_tests"])
    if "sources" not in value:
        return layer, False
    sources = value["sources"]
    if not isinstance(sources, dict):
        raise AppError(f"{label}: sources must be a mapping.")
    sources = deepcopy(sources)
    for source_id, settings in sources.items():
        validate_source_id(source_id, label)
        if not isinstance(settings, dict):
            raise AppError(f"{label}: sources.{source_id} must be a mapping.")
        validate_part(settings, defaults, f"{label}.sources.{source_id}")
        if major == 2:
            for key, folder in (("data_root", "data"), ("state_root", "state")):
                if settings.get(key) is None:
                    settings[key] = str(legacy_root() / folder / source_id)
    layer["sources"] = sources
    return layer, False


def resolve_source(settings: dict[str, Any], source_id: str, cwd: Path) -> None:
    for key, fallback in (
        ("data_root", user_root() / "data" / source_id),
        ("state_root", user_root() / "state" / source_id),
        ("notes_root", Path(settings["data_root"] or user_root() / "data" / source_id) / "notes"),
    ):
        path = Path(settings[key]) if settings[key] is not None else fallback
        path = path.expanduser()
        settings[key] = str((path if path.is_absolute() else cwd / path).resolve())
    settings["azure"]["prefix"] = settings["azure"]["prefix"].rstrip("/")
    settings["s3"]["prefix"] = settings["s3"]["prefix"].rstrip("/")
    for group, key in (("azure", "account_url"), ("s3", "endpoint_url"), ("delivery", "url_base")):
        if settings[group][key]:
            settings[group][key] = settings[group][key].rstrip("/")
    validate_provider(settings, f"sources.{source_id}")
    data, state, notes = (Path(settings[k]) for k in ("data_root", "state_root", "notes_root"))
    if overlaps(data, state):
        raise AppError(f"sources.{source_id}: data_root and state_root must be separate trees.")
    for reserved in ("staging", "originals", "releases", "catalog", "provenance", "history"):
        if overlaps(notes, data / reserved):
            raise AppError(f"sources.{source_id}: notes_root overlaps managed image/catalog trees.")
    if overlaps(notes, state):
        raise AppError(f"sources.{source_id}: notes_root must not overlap state_root.")


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def validate_isolation(sources: dict[str, Any]) -> None:
    previous: list[tuple[str, dict[str, Any]]] = []
    containers: dict[tuple[str, str, str], str] = {}
    for source_id, settings in sources.items():
        provider = settings["provider"]
        storage = settings["azure"] if provider == "azure" else settings["s3"]
        endpoint = storage.get("account_url") if provider == "azure" else storage["endpoint_url"]
        bucket = storage.get("container") if provider == "azure" else storage["bucket"]
        if bucket:
            target = (provider, (endpoint or "aws").lower(), bucket)
            if target in containers:
                raise AppError(
                    f"sources.{source_id} and sources.{containers[target]} use the same container/bucket; "
                    "one container or bucket must belong to one source, even with different prefixes."
                )
            containers[target] = source_id
        for other_id, other in previous:
            for key in ("data_root", "state_root", "notes_root"):
                for other_key in ("data_root", "state_root", "notes_root"):
                    if overlaps(Path(settings[key]), Path(other[other_key])):
                        raise AppError(
                            f"sources.{source_id}.{key} overlaps sources.{other_id}.{other_key}; "
                            "sources must use separate directory trees."
                        )
        previous.append((source_id, settings))


@dataclass(frozen=True)
class Config:
    values: dict[str, Any]
    loaded_sources: list[dict[str, Any]]
    origins: dict[str, str]
    source_id: str | None = None

    @property
    def sources(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.values["sources"])

    def select_source(self, source_id: str | None = None) -> Config:
        selected = source_id if source_id is not None else self.source_id
        if selected is None:
            if len(self.sources) != 1:
                raise AppError("Specify --source <id> when multiple or no sources are configured.")
            selected = next(iter(self.sources))
        if selected not in self.sources:
            raise AppError(
                f"Unknown source {selected!r}; available sources: {', '.join(self.sources)}."
            )
        return replace(self, source_id=selected)

    @property
    def source_values(self) -> dict[str, Any]:
        selected = self.select_source().source_id
        assert selected is not None
        return cast(dict[str, Any], self.sources[selected])

    @property
    def data_root(self) -> Path:
        return Path(self.source_values["data_root"])

    @property
    def state_root(self) -> Path:
        return Path(self.source_values["state_root"])

    @property
    def notes_root(self) -> Path:
        return Path(self.source_values["notes_root"])

    @property
    def azure(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.source_values["azure"])

    @property
    def provider(self) -> str:
        return cast(str, self.source_values["provider"])

    @property
    def s3(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.source_values["s3"])

    @property
    def storage(self) -> dict[str, Any]:
        return self.azure if self.provider == "azure" else self.s3

    @property
    def target_identity(self) -> dict[str, Any]:
        if self.provider == "azure":
            # Keep the original fingerprint so existing Azure baselines remain usable.
            return {key: self.azure[key] for key in ("account_url", "container", "prefix")}
        return {
            "provider": self.provider,
            "endpoint_url": (self.s3["endpoint_url"] or "aws").lower(),
            "bucket": self.s3["bucket"],
            "prefix": self.s3["prefix"],
        }

    @property
    def conversion(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.source_values["conversion"])

    @property
    def delivery(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.source_values["delivery"])

    def report(self) -> dict[str, Any]:
        return {
            "config": self.values,
            "loaded_sources": self.loaded_sources,
            "origins": self.origins,
            "selected_source": self.source_id,
            "effective_schema_version": CONFIG_SCHEMA_VERSION,
        }


def load_config(
    explicit: Path | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    source: str | None = None,
) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    if home is None:
        home = user_root()
        if not (home / "config.yaml").exists() and (legacy_root() / "config.yaml").exists():
            home = legacy_root()
    packaged = parse_yaml(resource("config.example.yaml"), "built-in")
    defaults = packaged["sources"][DEFAULT_SOURCE_ID]
    normalize_layer(packaged, defaults, "built-in")
    values = deepcopy(packaged)
    origins = {key: "built-in" for key in flatten(values)}
    loaded = [{"source": "built-in", "schema_version": CONFIG_SCHEMA_VERSION, "migrated": False}]
    candidates = [(home / "config.yaml", False), (cwd / ".tkn" / "config.yaml", False)]
    if explicit is not None:
        candidates.append((explicit.expanduser().resolve(), True))
    legacy_active = False
    for path, required in candidates:
        if not path.exists():
            if required:
                raise AppError(f"Explicit config not found: {path}")
            continue
        raw = parse_yaml(path.read_text(encoding="utf-8-sig"), str(path))
        layer, migrated = normalize_layer(raw, defaults, str(path))
        if "integration_tests" in layer:
            # Replace the whole mapping: never combine endpoint/hash across layers.
            values["integration_tests"] = layer["integration_tests"]
            origins = {
                key: item
                for key, item in origins.items()
                if key != "integration_tests" and not key.startswith("integration_tests.")
            }
            origins.update(
                {key: str(path) for key in flatten(layer["integration_tests"], "integration_tests")}
                or {"integration_tests": str(path)}
            )
        if "sources" in layer:
            if migrated:
                if not legacy_active:
                    values["sources"] = {LEGACY_SOURCE_ID: deepcopy(defaults)}
                    values["sources"][LEGACY_SOURCE_ID].update(
                        data_root=str(legacy_root() / "data"),
                        state_root=str(legacy_root() / "state"),
                    )
                    origins = {
                        key: item for key, item in origins.items() if not key.startswith("sources")
                    }
                    origins.update(
                        {key: "built-in" for key in flatten(values["sources"], "sources")}
                    )
                settings = layer["sources"][LEGACY_SOURCE_ID]
                merge(values["sources"][LEGACY_SOURCE_ID], settings)
                origins.update(
                    {key: str(path) for key in flatten(settings, f"sources.{LEGACY_SOURCE_ID}")}
                )
                legacy_active = True
            else:
                # Match excel_note: the complete sources mapping replaces the preceding one.
                values["sources"] = {}
                origins = {
                    key: item for key, item in origins.items() if not key.startswith("sources")
                }
                for source_id, settings in layer["sources"].items():
                    effective = deepcopy(defaults)
                    merge(effective, settings)
                    values["sources"][source_id] = effective
                    prefix = f"sources.{source_id}"
                    origins.update({key: "built-in" for key in flatten(defaults, prefix)})
                    origins.update({key: str(path) for key in flatten(settings, prefix)})
                if not layer["sources"]:
                    origins["sources"] = str(path)
                legacy_active = False
        loaded.append(
            {
                "source": str(path),
                "schema_version": raw["schema_version"],
                "migrated": migrated or raw["schema_version"].startswith("2."),
            }
        )
    config = Config(values, loaded, origins)
    if source is not None or len(config.sources) == 1:
        config = config.select_source(source)
    if overrides:
        validate_part(overrides, defaults, "CLI")
        config = config.select_source()
        merge(config.source_values, overrides)
        origins.update({key: "CLI" for key in flatten(overrides, f"sources.{config.source_id}")})
    for source_id, settings in config.sources.items():
        resolve_source(settings, source_id, cwd)
    validate_isolation(config.sources)
    return config


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
