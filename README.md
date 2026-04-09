# rspacectl

A command-line interface for [RSpace](https://www.researchspace.com/) covering both the electronic lab notebook and the inventory system. Built on the [rspace-client Python SDK](https://github.com/rspace-os/rspace-client-python).

> **Status:** Alpha — not yet published to PyPI. Install locally using the instructions below.

## Features

- **Verb-first command structure** — `rspace list documents`, `rspace get SA123`, `rspace create sample`
- **Full ELN coverage** — documents, notebooks, folders, forms, files, groups, users, activity
- **Full Inventory coverage** — samples, subsamples, containers, templates, workbenches
- **Smart `get` command** — infers resource type from GlobalID prefix (`SD`, `SA`, `SS`, `IC`, …)
- **Tagging** — set tags on any resource type with a single `rspace tag` command
- **Multiple output formats** — rich tables (default), JSON, CSV, quiet (IDs only, for piping)
- **Pagination** — all list commands support `--page` / `--page-size` with a result footer
- **Named profiles** — manage multiple RSpace instances with `--profile`
- **Keychain storage** — store credentials in the OS keychain, never in a plain-text file

## Requirements

- Python 3.9 or later
- [Poetry](https://python-poetry.org/) (for local installation)

## Local installation

The package is not yet published to PyPI. Install it directly from the repository using Poetry or pip.

### Option A — Poetry (recommended for development)

```bash
git clone https://github.com/rspace-os/rspacectl.git
cd rspacectl
poetry install
```

This installs the `rspace` command into a Poetry-managed virtual environment. Run commands via:

```bash
poetry run rspace --help
```

Or activate the environment first and use `rspace` directly:

```bash
poetry shell
rspace --help
```

### Option B — pip editable install

```bash
git clone https://github.com/rspace-os/rspacectl.git
cd rspacectl
pip install -e .
rspace --help
```

With optional keychain support:

```bash
pip install -e ".[keychain]"
```

## Configuration

### Interactive setup (recommended)

```bash
rspace configure
```

Saves credentials to `~/.rspacectl` with permissions `600`.

### Named profiles

Manage multiple RSpace instances (e.g. production and staging) with named profiles:

```bash
rspace configure --profile staging       # saves to ~/.rspacectl.staging
rspace --profile staging list samples   # uses staging credentials
rspace configure --list                 # show all configured profiles
```

The default profile (`~/.rspacectl`) is used when `--profile` is omitted — fully backward compatible.

### OS keychain storage

For better security, store credentials in the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) instead of a plain-text file. Requires the optional `keyring` dependency:

```bash
pip install -e ".[keychain]"        # or: poetry install -E keychain

rspace configure --keychain         # store default profile in keychain
rspace configure --profile prod --keychain   # named profile in keychain
```

Credentials stored in the keychain never appear in files, environment variables, or process arguments — recommended for agentic and CI use.

### Credential resolution order

For every invocation, credentials are resolved in this order (first match wins):

1. `--url` / `--api-key` CLI flags
2. `RSPACE_URL` / `RSPACE_API_KEY` environment variables
3. OS keychain
4. Profile dotenv file (`~/.rspacectl` or `~/.rspacectl.<profile>`)

### Manual config file

Create `~/.rspacectl`:

```dotenv
RSPACE_URL=https://your-rspace-instance.com
RSPACE_API_KEY=your-api-key-here
```

### Verify connection

```bash
rspace status
```

---

## Usage

### Output formats

Every command accepts `-o` / `--output`:

| Format | Flag | Description |
|--------|------|-------------|
| Table  | `-o table`  | Rich formatted table (default) |
| JSON   | `-o json`   | Pretty-printed JSON |
| CSV    | `-o csv`    | RFC 4180 CSV to stdout |
| Quiet  | `-o quiet`  | IDs only, one per line — useful for piping |

```bash
# Pipe document IDs into another command
rspace list documents -o quiet | xargs -I{} rspace get {}
```

---

### `list` — paginated listing

```bash
rspace list documents
rspace list documents --query "CRISPR" --tag "methods" --page 1 --page-size 50
rspace list documents --order-by name --sort-order asc

rspace list notebooks
rspace list folders --parent FL123

rspace list samples
rspace list samples --query "buffer" --owned-by alice --page 0 --page-size 20
rspace list subsamples --query "aliquot"
rspace list containers --query "freezer"
rspace list templates
rspace list files --type document

rspace list forms
rspace list groups
rspace list users                          # sysadmin only
rspace list activity --from 2024-01-01 --to 2024-03-31 --action CREATE
rspace list workbenches
```

A pagination footer is shown after every paginated result:

```
Showing 1–20 of 143  (page 0, use --page / --page-size to navigate)
```

---

### `get` — fetch a single resource

The resource type is inferred automatically from the GlobalID prefix:

```bash
rspace get SD123          # document
rspace get NB456          # notebook
rspace get FL789          # folder
rspace get SA101          # sample
rspace get SS202          # subsample
rspace get IC303          # container
rspace get BE404          # workbench
rspace get IT505          # sample template
rspace get FM606          # form
rspace get GL707          # gallery file
```

For plain numeric IDs, provide the type explicitly:

```bash
rspace get document 123
rspace get sample   456
```

Additional flags for richer output:

```bash
rspace get SA101 --subsamples     # sample detail + subsample list
rspace get IC303 --content        # container detail + contents
rspace get BE404 --content        # workbench detail + contents
rspace get NB456 --content        # notebook detail + document list
rspace get FL789 --content        # folder detail + document list
```

---

### `create` — create resources

```bash
rspace create document --name "My Experiment" --form FM123
rspace create notebook --name "Project Alpha"
rspace create folder   --name "Data" --parent FL456

rspace create sample --name "Buffer A" --template IT101
rspace create sample --from-csv samples.csv   # bulk create from CSV

rspace create container --name "Freezer 1" --type list
rspace create container --name "Box A"     --type grid --rows 9 --cols 9

rspace create user --username jdoe --email j.doe@example.com \
    --first-name Jane --last-name Doe        # sysadmin only; prompts for password
```

---

### `update` — modify existing resources

```bash
rspace update document SD123 --name "Revised Experiment"
rspace update document SD123 --tag "lab,2024,validated"
rspace update document SD123 --append "<p>Additional notes</p>"

# Target a specific form field
rspace update document SD123 --content "<p>New text</p>" --field-id 456
rspace update document SD123 --append "<p>Addendum</p>" --field-index 2

rspace update sample SA456 --name "Buffer A v2" --description "pH 7.4"
rspace update sample SA456 --tag "reagent,validated"
```

`--field-id` targets a field by its numeric ID (use `rspace get SD123 -o json` to find IDs).
`--field-index` targets a field by 0-based position (for `--append` / `--prepend`).

---

### `tag` — set tags on any resource

```bash
rspace tag SD123 "lab,experiment,2024"
rspace tag NB456 "methods,protocols"
rspace tag SA789 "reagent,validated"
rspace tag SS101 "aliquot,stock"
rspace tag IC202 "freezer,-80C"
rspace tag IT303 "template,approved"
```

Accepts any GlobalID — the resource type is inferred from the prefix. Replaces all existing tags.

---

### `delete` — delete resources

```bash
rspace delete document SD123 SD456 SD789   # batch delete
rspace delete sample    SA101
rspace delete subsample SS202 SS203
rspace delete container IC303
```

---

### `search` — full-text search across ELN and Inventory

```bash
rspace search "CRISPR protocol"
rspace search "buffer" --type inventory
rspace search "PCR"    --type eln
```

---

### `move` — move inventory items into a container

```bash
# List container
rspace move SS101 SS102 --target IC303

# Grid container — auto-fill by row or column
rspace move SS101 SS102 --target IC404 --strategy row
rspace move SS101 SS102 --target IC404 --strategy column

# Grid container — exact placement
rspace move SS101 --target IC404 --row 2 --col 3
```

---

### `upload` / `download`

```bash
# Upload a file to the gallery
rspace upload file ./data.csv

# Attach a file to an inventory item
rspace upload attachment ./spec.pdf SS101

# Download a gallery file
rspace download file GL606 --output-dir ./downloads

# Download an inventory attachment
rspace download attachment GL707 --output-dir ./downloads
```

---

### `split` — split a subsample

```bash
rspace split SS202 --count 4                      # split into 4 equal parts
rspace split SS202 --count 4 --target IC303       # split and place in container
```

---

### `share` — share a document with a group

```bash
rspace share SD123 --group-id 5 --permission read
```

---

### `export` — export ELN data

```bash
rspace export --format xml  --scope user
rspace export --format html --scope selection --id SD123 --id SD456
rspace export --format xml  --scope user --no-wait   # start job, don't wait
```

---

### `import` — import data

```bash
rspace import word  report.docx notes.docx --folder FL123
rspace import tree  ./my-lab-data --folder FL123
```

---

## GlobalID prefixes

| Prefix | Resource type   |
|--------|-----------------|
| `SD`   | Document        |
| `NB`   | Notebook        |
| `FL`   | Folder          |
| `SA`   | Sample          |
| `SS`   | Subsample       |
| `IC`   | Container       |
| `IT`   | Sample template |
| `FM`   | Form            |
| `GL`   | Gallery file    |
| `BE`   | Workbench       |

---

## Development

```bash
# Install with dev dependencies
poetry install

# With optional keychain support
poetry install -E keychain

# Run tests
poetry run pytest

# Format and lint
poetry run black rspacectl tests
poetry run ruff check rspacectl tests
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
