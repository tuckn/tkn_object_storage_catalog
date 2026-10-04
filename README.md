# Tkn Azure Blob Note

Preserve original images, synchronize working copies with Azure Blob Storage,
and manage their descriptions and relationships as Obsidian Markdown notes.

For example, importing a PNG preserves its original bytes, prepares a WebP for
upload, and creates a note where you can add a caption, tags, and links to related
projects. Uploading sends only the prepared image. Downloading retains exactly
the bytes stored in Azure. Neither operation requires anonymous public access.

## Scope and storage

The executable is `tkn-azure-blob-note`. Its stable application ID is
`azure_blob_note`. Runtime data belongs outside this source repository.

| Default location under `~/.tkn/azure_blob_note/` | Purpose |
| --- | --- |
| `config.yaml` | User settings |
| `data/staging/` | Optional input queue; `import` without arguments reads here |
| `data/originals/<sha256>/` | Immutable, verified source captures |
| `data/releases/` | Current image bytes corresponding to blob names |
| `data/notes/` | Markdown proxy notes and an Obsidian Bases view |
| `data/catalog/` | Asset IDs and technical file relationships |
| `data/provenance/` | Durable processing journals, including failures |
| `data/history/<sha256>/` | Previous local releases retained before replacement |
| `state/` | Synchronization baselines, run reports and logs |

Configure `data_root`, `state_root`, and optionally `notes_root` to choose other
locations. The defaults work independently of the installation directory.
Images, credentials, notes, and runtime records are not source-code assets.

## Setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Windows is the primary
platform. Python dependencies, including WebP conversion support through Pillow,
are installed together.

From the cloned repository:

```powershell
cd "C:\path\to\tkn_azure_blob_note"
uv tool install .
tkn-azure-blob-note --version
tkn-azure-blob-note --help
```

A version string confirms that the executable is installed. Help and version work
before configuration or Azure authentication. If the executable is not on PATH,
use `uv tool update-shell`, then open a new terminal.

Create the user configuration:

```powershell
tkn-azure-blob-note config init
tkn-azure-blob-note config list
```

Edit the displayed configuration file. This is an excerpt of the settings to change
for Azure; keep `schema_version` and other generated settings in that file:

```yaml
azure:
  account_url: https://examplestorage.blob.core.windows.net
  container: images
  prefix: ""
  auth: azure_cli
delivery:
  url_base: null
```

The account and container must already exist. The CLI does not create infrastructure,
change access permissions, provision a custom domain, or configure HTTPS.
For private images, leave `delivery.url_base` unset and use local image links.
A custom URL base must already serve the configured blob scope.

Local import, build, and note operations do not need Azure credentials.
For transfer and remote inspection, install Azure CLI and sign in:

```powershell
az login
```

Your identity needs blob data permissions for the intended operations, typically
Storage Blob Data Contributor for upload/download or Storage Blob Data Reader for
read-only work. See [Microsoft's authorization guidance](https://learn.microsoft.com/azure/storage/blobs/authorize-access-azure-active-directory).
Hosted execution can select `managed_identity` instead. Authentication secrets
are not stored in this application's YAML or notes. Runtime configuration never
loads `.env`.

## First image

Normal mutation commands perform their named operation. Add `--dry-run` to preview
without changing application data, notes, state, logs, or Azure.

Preserve and prepare one image:

```powershell
tkn-azure-blob-note import "C:\path\to\photo.png"
```

By default, a static JPEG or PNG becomes `releases/photo.webp`. The original remains
under `originals/<sha256>/`; the input file is retained too. The result includes
an asset ID and the release path. Other supported image formats and animated PNGs
are copied unchanged. To keep a JPEG/PNG unchanged, set `conversion.enabled: false`
or use `import --no-convert`.

Inspect the planned upload, then perform it:

```powershell
tkn-azure-blob-note push photo.webp --dry-run
tkn-azure-blob-note push photo.webp
tkn-azure-blob-note verify --remote
```

The upload is restricted to the configured container/prefix. Public or
unknown-access uploads request confirmation in an interactive terminal.
For an intentional public upload in automation, supply `--yes`.
Do not use this flag to resolve content conflicts; those require the separate
`--overwrite` option.

Open `data` as an Obsidian Vault and open the note under `notes`.
The default arrangement places the notes and local images in the same Vault.
Open `notes/images.base` (created by `notes refresh`) for a gallery.
Add descriptions, tags, `nouns`, `domains`, `projects`, and your own body text.
The CLI preserves these fields, unknown properties, comments, and text outside
its generated block.

To integrate with an existing Vault, set `notes_root` to a directory inside it.
External image references then use local file URIs: desktop preview depends on
your Obsidian environment, while the local link identifies the original file.
For reliable Vault-contained previews, keep the releases and notes within the Vault.
No plugin, junction, or automatic attachment duplication is installed.

## Daily use

```powershell
# Import a folder, preserving its relative directory structure.
tkn-azure-blob-note import "C:\path\to\images"

# Or process the configured data/staging queue.
tkn-azure-blob-note import

# Inspect changes locally, or against Azure.
tkn-azure-blob-note status
tkn-azure-blob-note status --remote

# Transfer all managed images in the configured scope.
tkn-azure-blob-note push
tkn-azure-blob-note pull

# Rebuild after changing conversion settings; retained originals are required.
tkn-azure-blob-note build

# Refresh generated metadata, URLs and local links.
tkn-azure-blob-note notes refresh

# Check local hashes and note identifiers.
tkn-azure-blob-note verify
```

Import does not consume/delete staging inputs. Repeating an import with the same
name, source bytes, and conversion recipe returns `unchanged`. A name collision
with different content stops. Use `--name photos/another.png` for a single input
to select a different path; conversion changes its suffix to `.webp`.

Titles and categories are independent of blob names. Renaming a proxy note inside
`notes_root` is supported through its stable IDs. Changing a title never renames
an image or changes its URL.

Push and pull compare prior synchronization state. If both sides changed, or an
untracked destination differs, the command stops. After examining both versions,
choose the desired direction explicitly:

```powershell
tkn-azure-blob-note pull photo.webp --overwrite --dry-run
tkn-azure-blob-note pull photo.webp --overwrite --yes
```

Previous local bytes are retained in `history` before replacement. Remote overwrite
recovery depends on your Azure versioning/backup settings; this CLI does not enable
them. No synchronization command deletes remote-only or local-only images.
A deleted remote image is not automatically recreated after an established sync.

## Command reference

Asset selectors accept an asset ID or relative release path. Omitted selectors
mean all applicable managed assets; omitted pull selectors mean all supported
images in the configured remote scope.

| Command | Input and result | Network / persistent changes |
| --- | --- | --- |
| `config init [--path FILE]` | Creates the packaged example; `--force` backs up an edited file before replacement | Local config only |
| `config list [--json]` | Effective settings, origins and schema versions | Read-only |
| `import [PATH ...] [--name PATH]` | Captures originals; prepares releases and notes | Local data and run records |
| `build [ASSET ...]` | Regenerates releases using retained originals | Local data/history and run records |
| `push [ASSET ...]` | Uploads/adopts matching blobs; records a sync baseline | Azure reads/writes; local state/notes |
| `pull [ASSET ...]` | Downloads exact blob bytes; creates or updates notes | Azure reads; local data/state/notes |
| `notes refresh [ASSET ...]` | Refreshes generated fields and creates a default Bases view if absent | Local notes/catalog and run records |
| `status [--remote]` | Reports local integrity and changes since the baseline | Read-only; optional Azure listing |
| `verify [--remote]` | Checks hashes, source captures and note IDs | Read-only; remote mode hashes downloaded bytes |
| `recover` | Completes interrupted local commits whose journaled bytes match | Local catalog/notes and run records |

Mutation commands accept `--dry-run`. Its transfer previews may authenticate,
list/get blobs and stream their content for hash comparison. Azure read operations
can incur service charges. Conversion, uploads, persistent downloads, application
logs, and state updates are suppressed. Authentication infrastructure may maintain
its own token caches outside application-owned storage.

All commands accept `--config`, `--data-root`, `--state-root`, `--notes-root`,
and mutually exclusive `--quiet` / `--verbose`. Settings may appear before or after
the subcommand. Human messages use stderr; operation results use JSON on stdout.
`config list` defaults to readable `key=value` lines and supports `--json`.
Exit codes: 0 success, 2 invalid input/conflict/verification failure, 3 Azure failure.
Argument errors also return 2.

## Details and limitations

- [Configuration, file contracts and synchronization rules](docs/reference/contracts.md)
- [Changelog](CHANGELOG.md)

The CLI manages images, not generic files or videos. It does not transcode WebM,
run AI classification, maintain an RDF database, delete Azure blobs, or automatically
rename deployed blob keys. Source captures and provenance can support future
graph export without requiring a graph service today.

Individual writes are atomic, but an entire batch is not a distributed transaction.
A failed batch can contain completed items. Review the run journal, run `verify`,
and use `recover` for an interrupted local commit before retrying the command.
A sync retry can recognize matching bytes after an interrupted upload. It never
assumes that a timestamp alone proves equality.

```powershell
tkn-azure-blob-note verify
tkn-azure-blob-note recover --dry-run
tkn-azure-blob-note recover
tkn-azure-blob-note verify
```

## Maintenance and development

After changing source, resources or dependencies, reinstall the normal tool:

```powershell
cd "C:\path\to\tkn_azure_blob_note"
uv tool install . --reinstall
tkn-azure-blob-note --version
```

After moving or renaming the repository folder, run the reinstall command above
from its new location to update uv's recorded source path. If you use the
repository's development environment, also run `uv sync --locked` there to refresh
its editable installation, which contains an absolute source path. The command
name and application storage under `~/.tkn/azure_blob_note/` stay the same.

Developer checks:

```powershell
cd "C:\path\to\tkn_azure_blob_note"
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Tests use temporary local data and a simulated Blob Storage adapter; they do not
write to a live account. The package uses a `src` layout, includes templates in the
wheel/sdist, and has no runtime dependency on this checkout. If immediate source
reflection is needed during development, use `uv tool install -e . --reinstall`.
Keep private settings, notes, images, and investigation reports out of commits.
