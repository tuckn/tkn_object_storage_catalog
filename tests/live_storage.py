"""Explicit opt-in live tests; never collected by ordinary pytest runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4
from xml.etree import ElementTree

PROJECT = Path(__file__).resolve().parents[1]
# Exercise this checkout even when a moved virtualenv has an old editable-install path.
sys.path.insert(0, str(PROJECT / "src"))

import boto3  # noqa: E402
from azure.core.exceptions import HttpResponseError  # noqa: E402
from botocore.config import Config as SDKConfig  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from PIL import Image  # noqa: E402

from tkn_objstorage_imgcatalog import cli  # noqa: E402
from tkn_objstorage_imgcatalog import config as configuration  # noqa: E402
from tkn_objstorage_imgcatalog.catalog import Catalog, make_record  # noqa: E402
from tkn_objstorage_imgcatalog.errors import AppError  # noqa: E402
from tkn_objstorage_imgcatalog.io import sha256  # noqa: E402
from tkn_objstorage_imgcatalog.notes import find_note, serialize, split_note  # noqa: E402
from tkn_objstorage_imgcatalog.storage import open_store  # noqa: E402

RUN_PATTERN = r"\d{8}T\d{6}Z-[0-9a-f]{32}"
RELATIVES = frozenset({"nested/生成 画像.webp", "adapter/条件 画像.png"})
CACHE = "private, max-age=0"


def require(condition):
    if not condition:
        raise AssertionError("Live test check failed; see the named check in report.json.")


def target_digest(target):
    payload = json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def load_test_target(path, name):
    """Read exactly the selected config file; ignore CWD and legacy fallback files."""
    raw = configuration.parse_yaml(path.read_text(encoding="utf-8-sig"), "test config")
    packaged = configuration.parse_yaml(configuration.resource("config.example.yaml"), "built-in")
    layer, _ = configuration.normalize_layer(
        raw, packaged["sources"][configuration.DEFAULT_SOURCE_ID], "test config"
    )
    targets = layer.get("integration_tests", {})
    if name not in targets:
        raise AppError("Requested integration_tests target is not configured.")
    target = deepcopy(targets[name])
    expected = target.pop("expected_target_sha256")
    validate_target(target, expected)
    return target, expected


def validate_target(target, expected_digest):
    require(isinstance(target, dict))
    try:
        configuration.validate_integration_tests(
            {"selected": target | {"expected_target_sha256": expected_digest}}, "live test"
        )
    except AppError:
        require(False)
    require(target_digest(target) == expected_digest)


def verify_s3_identity(target):
    """Use the dedicated profile, never injected R2 keys or a default profile."""
    session = boto3.Session(profile_name=target["profile"])
    with closing(
        session.client(
            "sts",
            region_name=target["region"],
            config=SDKConfig(ignore_configured_endpoint_urls=True),
        )
    ) as client:
        identity = client.get_caller_identity()
    role = target["role_arn"].split(":role/", 1)[1]
    expected = f"arn:aws:sts::{target['account_id']}:assumed-role/{role}/"
    require(identity.get("Account") == target["account_id"])
    arn = identity.get("Arn", "")
    require(
        arn.startswith(expected) and bool(arn[len(expected) :]) and "/" not in arn[len(expected) :]
    )


def run_prefix(run_id):
    require(isinstance(run_id, str) and re.fullmatch(RUN_PATTERN, run_id))
    return f"runs/{run_id}/"


def checked_key(run_id, relative):
    require(relative in RELATIVES)
    return run_prefix(run_id) + relative


def safe_error(exc):
    # Never include exception messages, SDK response bodies, URLs, or credentials.
    result = {"type": type(exc).__name__}
    status = None
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    elif isinstance(exc, HttpResponseError):
        status = exc.status_code
    if type(status) is int:
        result["http_status"] = status
    return result


def expect_http(action, statuses):
    try:
        action()
    except (ClientError, HttpResponseError) as exc:
        require(safe_error(exc).get("http_status") in statuses)
        return
    raise AssertionError("Conditional operation unexpectedly succeeded.")


def anonymous_denied(url, provider):
    try:
        with urlopen(url, timeout=30):
            raise AssertionError("Generated image was anonymously readable.")
    except HTTPError as exc:
        if provider != "s3" and exc.code in {401, 403}:
            return
        body = ElementTree.fromstring(exc.read(8192))
        if provider == "s3":
            require(exc.code == 403 and body.findtext("Code") == "AccessDenied")
        elif provider == "azure":
            require(exc.code == 409 and body.findtext("Code") == "PublicAccessNotPermitted")
        else:
            require(
                exc.code == 400
                and body.findtext("Code") == "InvalidArgument"
                and "Authorization" in (body.findtext("Message") or "")
            )


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def snapshot(config):
    return {
        str(path): (sha256(path), path.stat().st_mtime_ns)
        for root in (config.data_root, config.state_root, config.notes_root)
        for path in root.rglob("*")
        if path.is_file()
    }


@contextmanager
def isolated_settings(root):
    previous = Path.cwd()
    try:
        os.chdir(root)
        # Only settings discovery is isolated. CLI, conversion, sync and SDKs are real.
        with (
            patch.object(configuration, "user_root", return_value=root / "empty-user"),
            patch.object(configuration, "legacy_root", return_value=root / "empty-legacy"),
        ):
            yield
    finally:
        os.chdir(previous)


def cleanup_r2(target, expected_digest, run_id, manifest, store):
    require(target["provider"] == "r2")
    return cleanup_manifest(target, expected_digest, run_id, manifest, store)


def cleanup_s3(target, expected_digest, run_id, manifest, store):
    require(target["provider"] == "s3")
    require(store.config.s3["profile"] == target["profile"])
    require(store.config.s3["region"] == target["region"])
    return cleanup_manifest(target, expected_digest, run_id, manifest, store)


def cleanup_manifest(target, expected_digest, run_id, manifest, store):
    validate_target(target, expected_digest)
    require(target["provider"] in {"r2", "s3"})
    require(
        store.config.target_identity
        == {
            "provider": target["provider"],
            "endpoint_url": target["endpoint_url"] or "aws",
            "bucket": target["bucket"],
            "prefix": run_prefix(run_id).rstrip("/"),
        }
    )
    # Validate the entire manifest before issuing even the first delete.
    for relative, entry in manifest.items():
        require(entry["key"] == checked_key(run_id, relative))
        require(entry["sha256"] and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in entry["sha256"]))
        require(isinstance(entry["asset_id"], str) and entry["asset_id"])
    deleted = 0
    for relative, entry in manifest.items():
        remote = store.get(relative)
        if remote is None:
            continue
        # A colliding/replaced object is retained, never swept up by prefix deletion.
        require(remote["metadata"].get("asset_id") == entry["asset_id"])
        require(store.digest(relative, remote["etag"]) in entry["sha256"])
        options = {"IfMatch": remote["etag"]} if target["provider"] == "s3" else {}
        store.client.delete_object(Bucket=target["bucket"], Key=entry["key"], **options)
        deleted += 1
    return deleted


class LiveRun:
    def __init__(self, target, expected_digest, output_root):
        validate_target(target, expected_digest)
        self.target = target
        self.expected_digest = expected_digest
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex
        self.prefix = run_prefix(self.run_id)
        self.root = output_root.resolve() / self.run_id
        self.root.mkdir(parents=True, exist_ok=False)
        # Deep checkout + SHA256 originals + atomic temp names can exceed MAX_PATH.
        # Keep data/state/notes in a short, unique workspace and durable reports here.
        self.work_root = Path(tempfile.mkdtemp(prefix="osc-live-")).resolve()
        write_json(self.root / "workspace.json", {"work_root": str(self.work_root)})
        self.manifest = {}
        self.store = None
        self.report = {
            "schema_version": 1,
            "provider": target["provider"],
            "run_id": self.run_id,
            "target_sha256": expected_digest,
            "prefix": self.prefix,
            "status": "running",
            "checks": [],
            "cleanup": {"policy": target["cleanup"], "status": "pending"},
            "unverified": [
                "credential refresh beyond one-hour role session",
                "managed identity",
                "lifecycle timing and restore",
                "large/multipart transfers",
                "load/concurrency",
                "billing",
                "public delivery and infrastructure configuration",
                "installed console-script launcher",
            ],
        }
        self.save()

    def save(self):
        write_json(self.root / "manifest.json", {"run_id": self.run_id, "objects": self.manifest})
        write_json(self.root / "report.json", self.report)

    def check(self, name, action):
        entry = {"name": name, "status": "running"}
        self.report["checks"].append(entry)
        self.save()
        try:
            result = action()
            entry["status"] = "passed"
            return result
        except Exception as exc:
            entry.update(status="failed", error=safe_error(exc))
            raise
        finally:
            self.save()

    def settings(self, label):
        root = self.work_root / label
        root.mkdir()
        source = {
            "provider": self.target["provider"],
            **{f"{name}_root": str(root / name) for name in ("data", "state", "notes")},
            "delivery": {"url_base": None, "cache_control": CACHE},
            "conversion": {"enabled": True, "lossless": True},
        }
        if self.target["provider"] == "azure":
            source["azure"] = {
                "account_url": self.target["endpoint_url"],
                "container": self.target["bucket"],
                "auth": "azure_cli",
                "prefix": self.prefix.rstrip("/"),
            }
        else:
            source["s3"] = {
                "endpoint_url": self.target["endpoint_url"],
                "bucket": self.target["bucket"],
                "region": self.target.get("region", "auto"),
                "profile": self.target.get("profile"),
                "prefix": self.prefix.rstrip("/"),
            }
        path = root / "config.yaml"
        # JSON is a YAML subset; this file contains only non-secret test settings.
        write_json(path, {"schema_version": "3.0.0", "sources": {"integration": source}})
        config = configuration.load_config(
            path, home=self.work_root / "empty-user", cwd=self.work_root
        )
        return path, config

    def command(self, path, *args, expected=0):
        stdout, stderr = StringIO(), StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch.object(sys, "stdin", StringIO()),
        ):
            code = cli.main(["--config", str(path), "--quiet", *map(str, args)])
        require(code == expected)
        return json.loads(stdout.getvalue())

    def remember(self, relative, record):
        entry = self.manifest.setdefault(
            relative,
            {
                "key": checked_key(self.run_id, relative),
                "asset_id": record["asset_id"],
                "sha256": [],
            },
        )
        require(entry["asset_id"] == record["asset_id"])
        entry["sha256"].append(record["release"]["sha256"])
        # Persist before a write: partial upload failures still have bounded cleanup candidates.
        self.save()

    def remote_keys(self):
        if self.target["provider"] in {"r2", "s3"}:
            pages = self.store.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.target["bucket"], Prefix=self.prefix
            )
            return [obj["Key"] for page in pages for obj in page.get("Contents", [])]
        return [obj.name for obj in self.store.container.list_blobs(name_starts_with=self.prefix)]

    def scenario(self):
        path, config = self.settings("sender")
        pull_path, pull_config = self.settings("receiver")
        self.store = open_store(config)
        self.check("new run prefix is empty", lambda: require(not self.remote_keys()))
        if self.target["provider"] == "s3":
            self.check(
                "missing HEAD returns 404",
                lambda: expect_http(
                    lambda: self.store.client.head_object(
                        Bucket=self.target["bucket"], Key=self.prefix + "nested/生成 画像.webp"
                    ),
                    {404},
                ),
            )
            self.check(
                "adapter maps missing HEAD to None",
                lambda: require(self.store.get("nested/生成 画像.webp") is None),
            )
        source = self.work_root / "generated.png"
        Image.new("RGB", (24, 16), (10, 80, 140)).save(source)
        self.check(
            "CLI import and WebP conversion",
            lambda: self.command(path, "import", source, "--name", "nested/生成 画像.png"),
        )
        record = Catalog(config).assets()[0]
        relative = record["relative_path"]
        require(relative == "nested/生成 画像.webp")
        before = snapshot(config)
        self.check("CLI push dry-run", lambda: self.command(path, "push", "--dry-run"))
        self.check(
            "push dry-run keeps local and remote unchanged",
            lambda: require(snapshot(config) == before and not self.remote_keys()),
        )
        if self.target["provider"] in {"r2", "s3"}:
            self.check(
                f"{self.target['provider'].upper()} unknown-public-access confirmation",
                lambda: require("confirmation" in self.command(path, "push", expected=2)["error"]),
            )
            self.check(
                "unconfirmed push creates no objects", lambda: require(not self.remote_keys())
            )
        self.remember(relative, record)
        self.check(
            "CLI push creates image",
            lambda: require(
                self.command(path, "push", "--yes")["result"][0]["status"] == "created"
            ),
        )
        remote = self.store.get(relative)
        self.check(
            "adapter list and SHA256",
            lambda: require(
                [r["relative_path"] for r in self.store.list()] == [relative]
                and self.store.digest(relative, remote["etag"]) == record["release"]["sha256"]
            ),
        )

        def headers():
            if self.target["provider"] in {"r2", "s3"}:
                raw = self.store.client.head_object(
                    Bucket=self.target["bucket"], Key=self.prefix + relative
                )
                content_type = raw["ContentType"]
            else:
                raw = self.store.container.get_blob_client(
                    self.prefix + relative
                ).get_blob_properties()
                content_type = raw.content_settings.content_type
            require(content_type == "image/webp" and remote["cache_control"] == CACHE)
            require(remote["metadata"]["asset_id"] == record["asset_id"])
            require(remote["metadata"]["sha256"] == record["release"]["sha256"])

        self.check("content type, cache control and metadata", headers)
        from tkn_objstorage_imgcatalog.notes import urls

        self.check(
            "existing generated object denies anonymous GET"
            + (" with 403 AccessDenied" if self.target["provider"] == "s3" else ""),
            lambda: anonymous_denied(urls(config, relative)[0], self.target["provider"]),
        )
        self.check(
            "repeat push is unchanged",
            lambda: require(
                self.command(path, "push", "--yes")["result"][0]["status"] == "unchanged"
                and self.store.get(relative)["etag"] == remote["etag"]
            ),
        )
        self.check(
            "CLI remote status",
            lambda: require(
                self.command(path, "status", "--remote")["items"][0]["remote"] == "unchanged"
            ),
        )
        self.check(
            "CLI remote verify", lambda: require(self.command(path, "verify", "--remote")["valid"])
        )
        before = snapshot(pull_config)
        self.check("CLI pull dry-run", lambda: self.command(pull_path, "pull", "--dry-run"))
        self.check(
            "pull dry-run keeps receiver empty",
            lambda: require(
                snapshot(pull_config) == before
                and not pull_config.data_root.exists()
                and not pull_config.state_root.exists()
                and not pull_config.notes_root.exists()
            ),
        )
        self.check(
            "CLI pull into isolated receiver",
            lambda: require(
                self.command(pull_path, "pull")["result"][0]["status"] == "created"
                and sha256(pull_config.data_root / "releases" / relative)
                == record["release"]["sha256"]
            ),
        )
        self.check(
            "repeat pull is unchanged",
            lambda: require(self.command(pull_path, "pull")["result"][0]["status"] == "unchanged"),
        )

        note = find_note(config, record)
        data, body = split_note(note.read_text(encoding="utf-8"))
        data["description"] = "Generated integration test annotation"
        data["integrationCustom"] = "preserve"
        note.write_text(serialize(data, body + "\nKeep this test annotation.\n"), encoding="utf-8")
        replacement = self.work_root / "replacement.webp"
        Image.new("RGB", (24, 16), (220, 15, 60)).save(replacement, lossless=True)
        changed = deepcopy(record)
        changed["release"].update(sha256=sha256(replacement), bytes=replacement.stat().st_size)
        self.remember(relative, changed)
        self.check(
            "adapter conditional update",
            lambda: self.store.upload(relative, replacement, changed, remote),
        )
        self.check(
            "CLI rejects divergent remote push",
            lambda: require(
                "Remote differs" in self.command(path, "push", "--yes", expected=2)["error"]
            ),
        )
        self.check(
            "CLI pulls remote update",
            lambda: require(
                self.command(path, "pull")["result"][0]["status"] == "updated"
                and sha256(Catalog(config).release(record)) == changed["release"]["sha256"]
            ),
        )

        def preserved_note():
            fields, text = split_note(note.read_text(encoding="utf-8"))
            require(
                fields["description"] == "Generated integration test annotation"
                and fields["integrationCustom"] == "preserve"
                and "Keep this test annotation." in text
                and fields["assetId"] == record["asset_id"]
            )

        self.check("pull preserves user metadata and note body", preserved_note)
        self.check(
            "CLI verify after remote update",
            lambda: require(self.command(path, "verify", "--remote")["valid"]),
        )

        relative = "adapter/条件 画像.png"
        record = make_record(
            relative,
            self.prefix + relative,
            source=None,
            digest=sha256(source),
            size=source.stat().st_size,
        )
        self.remember(relative, record)
        first = self.check(
            "adapter PNG create", lambda: self.store.upload(relative, source, record, None)
        )
        self.check(
            "duplicate create rejected",
            lambda: expect_http(
                lambda: self.store.upload(relative, source, record, None), {409, 412}
            ),
        )
        updated_file = self.work_root / "updated.png"
        Image.new("RGB", (24, 16), (12, 200, 60)).save(updated_file)
        updated = deepcopy(record)
        updated["release"].update(sha256=sha256(updated_file), bytes=updated_file.stat().st_size)
        self.remember(relative, updated)
        previous = deepcopy(first)
        previous["metadata"]["custom"] = "preserved"
        second = self.check(
            "conditional PNG update and custom metadata",
            lambda: self.store.upload(relative, updated_file, updated, previous),
        )
        self.check(
            "updated ETag, metadata and SHA256",
            lambda: require(
                second["etag"] != first["etag"]
                and second["metadata"]["custom"] == "preserved"
                and self.store.digest(relative, second["etag"]) == updated["release"]["sha256"]
            ),
        )
        self.check(
            "stale ETag PUT rejected",
            lambda: expect_http(lambda: self.store.upload(relative, source, record, first), {412}),
        )
        self.check(
            "stale ETag GET rejected",
            lambda: expect_http(lambda: self.store.download(relative, first["etag"]), {412}),
        )
        self.check(
            "rejected write preserves current bytes",
            lambda: require(
                self.store.get(relative)["etag"] == second["etag"]
                and self.store.digest(relative, second["etag"]) == updated["release"]["sha256"]
            ),
        )

    def execute(self):
        failed = False
        try:
            if self.target["provider"] == "r2":
                require(
                    bool(os.environ.get("AWS_ACCESS_KEY_ID"))
                    and bool(os.environ.get("AWS_SECRET_ACCESS_KEY"))
                )
                require(
                    not os.environ.get("AWS_PROFILE") and not os.environ.get("AWS_SESSION_TOKEN")
                )
            if self.target["provider"] == "s3":
                self.check(
                    "dedicated profile STS account and role",
                    lambda: verify_s3_identity(self.target),
                )
            with isolated_settings(self.work_root):
                self.scenario()
        except Exception as exc:
            failed = True
            self.report["error"] = safe_error(exc)
        finally:
            cleanup = self.report["cleanup"]
            if self.store is not None:
                try:
                    if self.target["provider"] in {"r2", "s3"}:
                        if self.target["provider"] == "s3":
                            cleanup["method"] = "manifest-and-IfMatch"
                        cleaner = cleanup_s3 if self.target["provider"] == "s3" else cleanup_r2
                        cleanup["deleted"] = cleaner(
                            self.target,
                            self.expected_digest,
                            self.run_id,
                            self.manifest,
                            self.store,
                        )
                    remaining = self.remote_keys()
                    cleanup["remaining"] = len(remaining)
                    if self.target["provider"] in {"r2", "s3"}:
                        require(not remaining)
                        cleanup["status"] = "passed"
                    else:
                        cleanup["status"] = "retained_for_lifecycle"
                except Exception as exc:
                    cleanup.update(status="failed", error=safe_error(exc))
                    failed = True
                finally:
                    try:
                        self.store.close()
                    except Exception as exc:
                        self.report["close_error"] = safe_error(exc)
                        failed = True
            else:
                cleanup["status"] = "not_connected"
            self.report["status"] = "failed" if failed else "passed"
            self.report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            self.save()
        return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run",
        action="store_true",
        help="authorize writes of generated images; R2/S3 also clean their manifests",
    )
    mode.add_argument(
        "--check", action="store_true", help="validate settings without cloud or writes"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=configuration.user_root() / "config.yaml",
        help="config.yaml containing integration_tests (default: application user config)",
    )
    parser.add_argument(
        "--test-target",
        required=True,
        help="entry name under integration_tests, e.g. azure, r2 or s3",
    )
    args = parser.parse_args(argv)
    # SDK HTTP logs are never useful in credential-bearing live test processes.
    logging.disable(logging.CRITICAL)
    try:
        target, expected = load_test_target(args.config.expanduser(), args.test_target)
        if args.check:
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "test_target": args.test_target,
                        "provider": target["provider"],
                        "target_sha256": expected,
                    }
                )
            )
            return 0
        run = LiveRun(target, expected, PROJECT / ".local" / "integration")
        code = run.execute()
        print(
            json.dumps(
                {
                    "provider": target["provider"],
                    "run_id": run.run_id,
                    "status": run.report["status"],
                    "checks": len(run.report["checks"]),
                    "cleanup": run.report["cleanup"],
                }
            )
        )
        return code
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": safe_error(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
