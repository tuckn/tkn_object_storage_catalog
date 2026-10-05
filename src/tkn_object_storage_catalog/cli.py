from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from azure.core.exceptions import AzureError, HttpResponseError
from botocore.exceptions import BotoCoreError, ClientError

from . import __version__
from .catalog import Operation
from .config import flatten, init_config, legacy_root, load_config, user_root
from .errors import AppError
from .images import build_images, import_images
from .notes import refresh_notes
from .recovery import recover
from .storage import open_store
from .sync import pull, push, status, verify

LOGGER = logging.getLogger("tkn_object_storage_catalog")
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")


class ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__("[%(levelname)s] %(message)s")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.color:
            code = {10: "90", 20: "36", 25: "32", 30: "33", 40: "31", 50: "31;1"}.get(
                record.levelno, "0"
            )
            return f"\x1b[{code}m{text}\x1b[0m"
        return text


def setup_logging(quiet: bool, verbose: bool) -> None:
    LOGGER.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    color = bool(
        sys.stderr.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    )
    if color and os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-12)
        mode = ctypes.c_ulong()
        color = bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 4)
        )
    handler.setFormatter(ConsoleFormatter(color))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.ERROR if quiet else logging.DEBUG if verbose else logging.INFO)
    LOGGER.propagate = False


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source",
        default=argparse.SUPPRESS,
        help="source ID; required for data commands when multiple sources are configured",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="explicit YAML override (merged after user/CWD config)",
    )
    parser.add_argument(
        "--data-root",
        default=argparse.SUPPRESS,
        help="override image/data directory for this invocation",
    )
    parser.add_argument(
        "--state-root", default=argparse.SUPPRESS, help="override operation/sync state directory"
    )
    parser.add_argument(
        "--notes-root", default=argparse.SUPPRESS, help="override proxy-note directory"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    group.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)


def mutating(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview only: no persistent files, logs, conversion or remote writes; sync may authenticate/read/download for comparison",
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="tkn-object-storage-catalog",
        description="Catalog images in Azure Blob Storage, AWS S3, and Cloudflare R2 with Obsidian metadata.",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common(root)
    sub = root.add_subparsers(dest="command", required=True)
    conf = sub.add_parser("config", help="create or inspect YAML settings")
    common(conf)
    cfg = conf.add_subparsers(dest="config_command", required=True)
    init = cfg.add_parser("init", help="create user config; existing edits are protected")
    common(init)
    init.add_argument("--path", type=Path, help="explicit destination instead of the user config")
    init.add_argument("--force", action="store_true", help="back up and replace existing config")
    mutating(init)
    listing = cfg.add_parser("list", help="read-only resolved settings, origins, schemas")
    common(listing)
    listing.add_argument("--json", action="store_true")
    imp = sub.add_parser(
        "import", help="preserve originals, prepare releases and notes; omitted input uses staging"
    )
    common(imp)
    imp.add_argument("paths", nargs="*", type=Path)
    imp.add_argument("--name", help="relative release filename for a single input")
    imp.add_argument(
        "--convert",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the selected source conversion.enabled",
    )
    mutating(imp)
    build = sub.add_parser(
        "build", help="rebuild releases from retained originals with current conversion settings"
    )
    common(build)
    build.add_argument(
        "assets", nargs="*", help="asset IDs or relative release paths; omitted means all"
    )
    mutating(build)
    for command in ("push", "pull"):
        item = sub.add_parser(
            command,
            help=("upload local releases" if command == "push" else "download exact object bytes"),
        )
        common(item)
        item.add_argument(
            "assets", nargs="*", help="asset IDs or relative paths; omitted means configured scope"
        )
        item.add_argument(
            "--overwrite",
            action="store_true",
            help="explicitly resolve differing/untracked content in this command's direction",
        )
        item.add_argument(
            "--yes", action="store_true", help="skip public upload/overwrite confirmation"
        )
        mutating(item)
    notes = sub.add_parser("notes", help="refresh generated fields while retaining human content")
    common(notes)
    nsub = notes.add_subparsers(dest="notes_command", required=True)
    refresh = nsub.add_parser("refresh")
    common(refresh)
    refresh.add_argument("assets", nargs="*")
    mutating(refresh)
    for command in ("status", "verify"):
        item = sub.add_parser(
            command,
            help="inspect local assets"
            if command == "status"
            else "validate hashes, originals, and note IDs",
        )
        common(item)
        item.add_argument(
            "--remote",
            action="store_true",
            help="also authenticate and read the selected object storage; verify downloads bytes for hashing",
        )
    recovery = sub.add_parser(
        "recover", help="finish interrupted local commits whose exact prepared bytes exist"
    )
    common(recovery)
    mutating(recovery)
    return root


def config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides = {
        key: getattr(args, key)
        for key in ("data_root", "state_root", "notes_root")
        if hasattr(args, key)
    }
    if getattr(args, "convert", None) is not None:
        overrides["conversion"] = {"enabled": args.convert}
    return overrides


def print_config(value: dict[str, Any]) -> None:
    for key, item in flatten(value).items():
        if isinstance(item, list):
            for index, part in enumerate(item):
                if isinstance(part, dict):
                    for inner, v in flatten(part).items():
                        print(f"{key}[{index}].{inner}={v}")
                else:
                    print(f"{key}[{index}]={part}")
        else:
            text = (
                "null"
                if item is None
                else str(item).lower()
                if isinstance(item, bool)
                else str(item)
            )
            escaped = text.replace(chr(10), r"\n").replace(chr(13), r"\r")
            print(f"{key}={escaped}")


def execute(args: argparse.Namespace) -> tuple[Any, int]:
    if args.command == "config" and args.config_command == "init":
        if (
            args.path is None
            and not (user_root() / "config.yaml").exists()
            and (legacy_root() / "config.yaml").exists()
        ):
            raise AppError(
                "An existing Azure config is in use. Inspect it with config list; to create a new config, use config init --path and preserve existing data_root/state_root/notes_root explicitly."
            )
        return init_config(
            (args.path or user_root() / "config.yaml").expanduser().resolve(),
            force=args.force,
            dry_run=args.dry_run,
        ), 0
    overrides = config_overrides(args)
    config = load_config(
        getattr(args, "config", None), overrides, source=getattr(args, "source", None)
    )
    if args.command == "config":
        LOGGER.info("Showing resolved configuration")
        if args.json:
            return config.report(), 0
        print_config(config.report())
        return None, 0
    config = config.select_source()
    remote = args.command in {"push", "pull"} or getattr(args, "remote", False)
    blobs = open_store(config) if remote else None
    try:
        if args.command == "status":
            return {"source_id": config.source_id, "items": status(config, blobs=blobs)}, 0
        if args.command == "verify":
            items = verify(config, blobs=blobs)
            return {
                "source_id": config.source_id,
                "items": items,
                "valid": not any(i["status"] == "failed" for i in items),
            }, (2 if any(i["status"] == "failed" for i in items) else 0)
        with Operation(config, args.command, args.dry_run) as operation:
            LOGGER.info(
                "%s%s (source %s)",
                "Previewing " if args.dry_run else "Running ",
                args.command,
                config.source_id,
            )
            if args.command == "import":
                output: Any = import_images(config, args.paths, operation, name=args.name)
            elif args.command == "build":
                output = build_images(config, args.assets, operation)
            elif args.command == "notes":
                output = refresh_notes(config, args.assets, dry_run=args.dry_run)
            elif args.command in {"push", "pull"}:
                assert blobs is not None
                func = push if args.command == "push" else pull
                try:
                    output = func(
                        config,
                        args.assets,
                        operation,
                        blobs,
                        overwrite=args.overwrite,
                        yes=args.yes,
                    )
                except AppError as exc:
                    if "confirmation" not in str(exc) or args.dry_run or not sys.stdin.isatty():
                        raise
                    print(str(exc) + " Continue? [y/N] ", file=sys.stderr, end="", flush=True)
                    if input().strip().lower() not in {"y", "yes"}:
                        raise AppError("Cancelled.") from exc
                    output = func(
                        config, args.assets, operation, blobs, overwrite=args.overwrite, yes=True
                    )
            else:
                output = recover(config, operation)
            return {
                "command": args.command,
                "source_id": config.source_id,
                "dry_run": args.dry_run,
                "run_id": None if args.dry_run else operation.run_id,
                "result": output,
            }, 0
    finally:
        if blobs:
            blobs.close()


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if getattr(args, "quiet", False) and getattr(args, "verbose", False):
        argument_parser.error("--quiet and --verbose are mutually exclusive.")
    setup_logging(getattr(args, "quiet", False), getattr(args, "verbose", False))
    try:
        result, code = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command != "config":
            LOGGER.log(
                SUCCESS if code == 0 else logging.ERROR,
                "%s %s",
                args.command,
                "completed" if code == 0 else "found inconsistencies",
            )
        return code
    except (AppError, OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    except AzureError as exc:
        # SDK exceptions may include request URLs/tokens; keep diagnostics bounded and credential-free.
        message = f"Azure request failed ({type(exc).__name__}). Check az login, data permissions, and network connectivity."
        if isinstance(exc, HttpResponseError) and exc.status_code in {409, 412}:
            message = "Azure changed during the operation. Inspect status --remote and retry after resolving the conflict."
        LOGGER.error("%s", message)
        print(json.dumps({"status": "failed", "error": message}))
        return 3
    except (BotoCoreError, ClientError) as exc:
        # Do not render SDK exception text: it may contain endpoints or credentials.
        message = "S3/R2 request failed. Check the selected profile/environment credentials, endpoint, region, permissions, and network connectivity."
        if isinstance(exc, ClientError) and exc.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        ) in {409, 412}:
            message = "S3/R2 changed during the operation. Inspect status --remote and retry after resolving the conflict."
        LOGGER.error("%s", message)
        print(json.dumps({"status": "failed", "error": message}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
