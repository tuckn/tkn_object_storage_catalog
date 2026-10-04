# Configuration and data contracts

## Settings

Configuration uses a separate schema version, currently `"1.0.0"`.
Each file must have this string before merging. Version 1.0.x patches are accepted;
newer minors/majors and unsupported old majors fail. Unknown keys, duplicate YAML
keys, wrong types, and credentials/query strings in configured URLs are rejected.
Reading settings never rewrites them.

Precedence, from weakest to strongest:

1. Packaged defaults.
2. `~/.tkn/azure_blob_note/config.yaml`.
3. `./.tkn/config.yaml` in the invocation's working directory.
4. An explicitly supplied `--config FILE`.
5. Corresponding CLI options.

Each source is validated independently. A malformed lower source cannot be hidden
by a valid override. Missing automatic sources are skipped; a missing explicit
source fails. There are no named profiles in this version. `.env` files are not loaded.

`~` expands to the current user's home. Relative settings resolve against the
invocation's working directory. Absolute paths are recommended for scheduled use.
Data and state roots cannot overlap. Notes cannot overlap internal image, catalog,
provenance, backup, or state trees.

| Key | Default | Meaning |
| --- | --- | --- |
| `schema_version` | `"1.0.0"` | Required per configuration file |
| `data_root` | `~/.tkn/azure_blob_note/data` | Persistent image library |
| `state_root` | `~/.tkn/azure_blob_note/state` | Operational records |
| `notes_root` | `null` | Resolves to `data_root/notes` |
| `azure.account_url` | `null` | HTTPS account endpoint, no path/query |
| `azure.container` | `null` | Existing container; required for Azure commands |
| `azure.prefix` | empty | Optional relative blob directory |
| `azure.auth` | `azure_cli` | `azure_cli` or `managed_identity` |
| `azure.managed_identity_client_id` | `null` | Optional user-assigned identity |
| `azure.timeout_seconds` | 60 | Positive server, socket and CLI-credential timeout; not a whole-batch deadline |
| `delivery.url_base` | `null` | Delivery URL corresponding to the configured prefix |
| `delivery.cache_control` | `null` | Cache-Control on upload; null preserves an existing blob's setting |
| `conversion.enabled` | true | Convert eligible static JPEG/PNG files to WebP |
| `conversion.format` | `webp` | The supported conversion format |
| `conversion.quality` | 82 | Integer from 0 through 100 |
| `conversion.lossless` | false | Use lossless WebP encoding |
| `conversion.strip_metadata` | true | Omit EXIF, XMP and ICC metadata from derivatives |

The converter applies EXIF orientation before encoding. It preserves transparency.
Animated PNGs pass through without conversion. GIF, APNG, AVIF, BMP, ICO, SVG,
TIFF and existing WebP files pass through unchanged; extension matching is
case-insensitive. Original captures preserve every byte even when metadata is
removed from derivatives. Conversion recipes fingerprint the options, Pillow
version, and WebP library version. A format change that would rename an existing
release stops; import a separately named asset instead.

## Azure scope and URLs

For `prefix: collection`, release `photos/example.webp` maps to blob
`collection/photos/example.webp`. The prefix boundary includes a slash; listing
`collection` never incorporates `collection-other`.

`delivery.url_base` maps directly to the configured scope. For example,
`https://img.example.com/collection` produces
`https://img.example.com/collection/photos/example.webp`.
This setting does not prove that the URL is reachable or public.
Without a custom base, `url` uses the native blob URL, which can require authentication.
URL path components are percent-encoded. SAS credentials are never written to notes.

The CLI leaves container ACLs unchanged. A custom delivery base, the `$web`
container, anonymous container access, or an ACL that cannot be inspected requires
confirmation before a changed upload. In particular, a private ACL on `$web`
does not make its static-website endpoint private.
See [Azure static website access](https://learn.microsoft.com/azure/storage/blobs/storage-blob-static-website#impact-of-setting-the-access-level-on-the-web-container).

## Asset and note identity

Catalog records at `catalog/<asset_id>.json` carry `schema_version`, a stable
asset ID, a separate note ID, relative release/note paths, timestamps, source
capture details, and the current release hash/recipe. Record timestamps use UTC
with explicit offsets. Human-readable titles are not identity keys.

Sources are captured under `originals/<sha256>/<original-filename>`.
SHA-256 identifies bytes; an asset ID identifies the managed image independently
of its filename. Original objects are not edited. Different content colliding at
one release path is refused on import.

Metadata ownership:

| Owner | Fields/content |
| --- | --- |
| Human / Obsidian | `type`, `title`, `category`, `description`, `tags`, `nouns`, `domains`, `projects`, unknown properties, body outside generated markers |
| CLI | `schemaVersion`, `assetId`, `noteId`, `localPath`, `releaseRef`, `sourceAvailable`, `sourceRef`, `originalRef`, `sourceSha256`, `sha256`, `bytes`, `blobName`, `blobUrl`, `url`, `cover`, `updated`, `syncStatus` |

Generated Frontmatter is flat. Unknown user fields are not flattened or discarded.
Fresh notes default to `type: image`. Operations use `syncStatus`, `url`,
`releaseRef` and catalog timestamps.

The generated block is bounded by `azure-blob-note:begin` and
`azure-blob-note:end` HTML comments. Refresh replaces only that block.
Unmarked user bodies are retained and receive a new block. Duplicate note IDs,
a different asset at the expected note path, unsupported note schemas, and
malformed markers stop the refresh. Notes renamed within `notes_root` are found
through their IDs; moving them outside that root requires explicit configuration
or relocation.

`syncStatus: synced` records the last completed transfer; it is not a live
assertion about remote state. Use `status --remote` or `verify --remote` for that.
Imported remote blobs without original captures have `sourceAvailable: false`. Downloading a transformed image does not recreate its
preconversion original.

## Synchronization and conflict handling

Each data-root/target scope has a local baseline containing an asset ID, relative blob name,
last synchronized SHA-256, ETag, optional Azure version ID, and synchronization time.
ETags detect remote revision changes; they are not content hashes.
Remote metadata hashes are not trusted as proof of equality. Untracked or changed
remote revisions are streamed and hashed before adoption.

- Equal bytes are adopted without another upload.
- Local-only changes can be pushed when the remote baseline is unchanged.
- Remote-only changes can be pulled when local bytes still match the baseline.
- Independently changed/untracked different bytes cause a conflict.
- `--overwrite` selects the command's direction for explicit conflict resolution.
  Changed replacements require `--yes` in noninteractive use.
- Updates use an ETag precondition; new uploads use non-overwriting creation.
  A competing write causes a conflict instead of last-writer-wins behavior.
- Pull performs an ETag-conditional download, verifies its bytes, archives the
  prior local release, then commits the new local file.
- Missing files are not propagated as deletions.

Windows-reserved names, traversal, absolute paths, backslashes in blob names,
trailing dots/spaces, linked managed paths, and case-colliding remote paths are
rejected. The CLI does not silently rename such blobs.

## Provenance, failures and backup

Each real mutating operation writes a durable incremental journal in
`data/provenance/<run_id>.json`, a run report under `state/runs`, and a UTF-8 log
under `state/logs`. The journal links source/release entities, tool version,
effective configuration fingerprint, operations, hashes, and timestamps.
Preparation records are persisted before local release replacement.
Each data root has an OS-backed operation lock.

These are structured application records, not RDF documents or a general graph
index. They provide an export boundary for future
[PROV-O Entity, Activity and Agent relationships](https://www.w3.org/TR/prov-o/).

Completed file writes use a sibling temporary file and atomic installation.
An entire multi-file operation can still be interrupted. Failed/running journals
remain inspectable. `recover` restores a prepared catalog/note commit only when
the exact journaled release and original hashes are present; it does not invent
missing bytes or perform cloud writes. Then rerun the relevant operation.
If note parsing still fails, fix or preserve that note before retrying.

Back up the complete data and state roots plus an external notes root.
The library's history/original captures do not replace an independent backup.
Deletion/retention automation is not provided. Application-owned dry runs create
none of these records. No AI service is called by this application.
