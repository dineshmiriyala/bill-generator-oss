# Bill Generator

A local-first invoicing and accounting app for small businesses and
print shops. Runs as a desktop window on Windows or as a normal web
app on macOS / Linux. All customer data stays on your machine.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#getting-started)

---

## What you can do with it

- Create invoices in three steps: pick customer → add items → print
- Save drafts, duplicate older bills, attach unpaid dues to a new bill
- Track customers and inventory with soft-delete + one-click restore
- Record payments and expenses against any customer or invoice
- Print or save-as-PDF for invoices and accounting statements
- Generate UPI QR codes for instant payments (India)
- See sales trends by day / month / year and top customers
- Automatic local backups, plus optional mirroring to a folder of your choice
- Optional: sync to your own Supabase project

---

## Getting started

Pick the option that matches your machine.

### Option A — Pre-built binary (easiest, no Python install)

> Recommended for shop owners who just want to use the app.

1. Go to the [Releases page](../../releases/latest) on GitHub and
   download the file for your OS:
   - **Windows:** `BillGenerator_V4.5.1.exe`
   - **macOS (Apple Silicon, M1/M2/M3/M4):** `BillGenerator_V4.5.1-macos-arm64.zip`
   - **macOS (Intel):** `BillGenerator_V4.5.1-macos-intel.zip`
2. Open it (your OS will show a security warning the **first time** —
   see [First launch — bypassing the OS warning](#first-launch--bypassing-the-os-warning)
   below).
3. The app opens in its own window. The onboarding wizard guides you
   through your first-time setup (see [First-time setup](#first-time-setup)).
4. Launch it again later from the Start menu / Applications folder.
   Your data is preserved between runs in your OS data directory (see
   [Where your data lives](#where-your-data-lives)).

To build the binary yourself from source, see
[Building locally](#building-locally-without-github-actions).

#### First launch — bypassing the OS warning

The downloaded binary is open-source and built transparently in public
on GitHub Actions ([the workflow](.github/workflows/release.yml) is
checked into this repo — anyone can audit it). But because it isn't
yet **code-signed** by a paid commercial certificate authority,
Windows and macOS treat it as "from an unknown publisher" the first
time you run it. This is the OS doing its job — not a problem with
the app. Here's how to get past it once:

**Windows — "Windows protected your PC"**

1. Double-click `BillGenerator_V4.5.1.exe`.
2. A blue window appears. Click **More info** (small text under the
   warning).
3. A **Run anyway** button appears at the bottom. Click it.
4. The app launches. You will not see the warning again on this machine.

**macOS — "BillGenerator cannot be opened"**

1. Unzip the downloaded `.zip`.
2. Drag `BillGenerator_V4.5.1.app` into your **Applications** folder.
3. **Right-click** (or Control-click) the app → **Open**.
4. A dialog says *"macOS cannot verify the developer..."* — click
   **Open** to confirm.
5. The app launches. From now on, double-click works normally.

If macOS quarantines the app and refuses to open it even with
right-click → Open (rare, but happens on newer macOS versions), open
**Terminal** and run:

```bash
xattr -dr com.apple.quarantine /Applications/BillGenerator_V4.5.1.app
```

Then try opening again.

### Option B — From source (development / customization)

> Recommended if you want to develop, customize, or run on Linux.

You'll need **Python 3.11 or later**. Check with `python3 --version`.

```bash
# 1. Get the code
git clone <your-fork-url>
cd bill-generator

# 2. Create a virtual environment
python3 -m venv .venv

# 3. Activate it
source .venv/bin/activate            # macOS / Linux
# .venv\Scripts\activate              # Windows PowerShell / cmd

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run the app
python app.py
```

Open <http://127.0.0.1:42069> in your browser. The onboarding wizard
appears on first launch — see [First-time setup](#first-time-setup).

To stop the app, press **Ctrl+C** in the terminal. To start it again
later, activate the venv and run `python app.py`.

---

## First-time setup

The onboarding wizard walks you through five short pages:

| Page | What you enter |
|------|----------------|
| 1. Business | Business name (e.g. *ABC Enterprises*) |
| 2. Owner & phone | Your name and contact number |
| 3. Details | Email, address, UPI ID, GSTIN, business type, PAN — most are optional |
| 4. Bank | Account name / number / IFSC / bank / branch (optional but recommended if you want bank details on invoices) |
| 5. Confirm | Review and finish |

Everything you enter is written to `info.json` on your machine. You
can edit it anytime from the **Account Settings** screen — no restart
required.

**Want to skip onboarding while testing?** Copy the bundled template:

```bash
cp db/info.example.json db/info.json
```

Then restart the app.

---

## Daily use — common tasks

### Create your first invoice

1. From the home page, click **Bill**.
2. Pick an existing customer, or click **+ Add new** if it's a first-time
   customer. New customers come back to the bill page automatically once
   saved.
3. Add line items: name, quantity, unit price, optional tax %. Use the
   **Round** button on a row to snap that line to the nearest 10.
4. (Optional) Toggle **Bill with Dues** to append the customer's older
   unpaid bills into one combined total.
5. Click **Generate Bill**. The printable A4 preview opens — use the
   browser's print dialog to print on paper or save as PDF.

Need to come back to it later? Use **Save Draft** instead of Generate.
Drafts live under **Draft Bills** on the home page and don't take up an
invoice number until you finalise them.

### Add a customer

Home → **Customer** → **Add New Customer**. Phone + company name is
checked for duplicates. Soft-deleted customers can be restored from
**Recovery** in the Account menu.

### Record a payment or expense

From the home page click **Add Transaction**. Pick the customer; for
payments, load that customer's bills and select one or more to auto-fill
the amount. Save — the transaction shows up immediately in that
customer's accounting page.

### See what a customer owes

Home → **Client Statement** → search the customer. You'll see total
billed, total paid, balance due, bill history with expandable items,
and a transaction log. Use **Print / Save PDF** at the top to export.

### Whole-company numbers

Home → **Company Books** → pick a date range. Two tabs:

- **Simple** — invoice rows and one total. Best for "what did I bill
  this month?"
- **Accounting Statement** — invoices plus the transaction ledger.
  Best for end-of-month reconciliation.

Both have **Print / Save PDF** buttons.

### Back up your data

Backups happen automatically: every Supabase sync and every weekly
freshness check creates a timestamped snapshot in `db/backups/`. The
ten most recent are kept; older ones are pruned.

To also mirror backups to a folder you choose (e.g. a USB drive or
Dropbox folder):

1. Go to **Account Settings** → set **File Location** to a path
   inside your home directory.
2. Click **More → Make Local DB Copy** anytime to trigger a manual
   mirror.

### Recover something you deleted

Home → **More** → **Recovery Centre**. Soft-deleted customers and
invoices are listed with a one-click restore button.

---

## Where your data lives

| OS | Default data directory |
|----|------------------------|
| Windows (packaged `.exe`) | `C:\Users\<you>\AppData\Roaming\Bill Generator\` |
| macOS (packaged) | `~/Library/Application Support/Bill Generator/` |
| Linux (packaged) | `~/.local/share/Bill Generator/` |
| From source (any OS) | `<repo>/db/` |

The directory contains your SQLite database (`app.db`), settings
(`info.json`), the persisted session secret (`secret.key`), and the
backups folder.

---

## Configuration

All branding and networking knobs are environment variables, prefixed
`BG_`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | What it does |
|----------|---------|--------------|
| `BG_APP_NAME` | `Bill Generator` | Window title, logs, packaging metadata |
| `BG_INVOICE_PREFIX` | `INV` | Invoice IDs look like `INV-280526-00042` |
| `BG_TXN_PREFIX` | `TXN` | Transaction IDs look like `TXN-280526-000001` |
| `BG_BIND_HOST` | `127.0.0.1` | Set `0.0.0.0` to expose on LAN — **read [SECURITY.md](SECURITY.md) first** |
| `BG_DESKTOP` | unset | Set to `1` for OS-specific data dir (set automatically by the desktop launcher) |
| `SECRET_KEY` | auto-generated | Flask session key; one is generated and persisted on first launch if unset |

Setting an env var:

```bash
# macOS / Linux
export BG_APP_NAME="ACME Invoicing"
python app.py

# Windows PowerShell
$env:BG_APP_NAME = "ACME Invoicing"
python app.py
```

---

## Releases via GitHub Actions

The repo ships with a [release workflow](.github/workflows/release.yml)
that builds Windows, macOS arm64 (Apple Silicon), and macOS Intel
binaries on GitHub's hosted runners. You don't need a Windows machine
or multiple Macs — push a version tag and the workflow does the rest.

### Cutting a release

```bash
# 1. Bump the version inside version.txt and build_exe.bat / build_macos.sh
#    (the APP_NAME constant), then commit:
git commit -am "Release v4.5.2"

# 2. Tag and push:
git tag v4.5.2
git push origin main --tags
```

GitHub Actions will:

1. Spin up `windows-latest`, `macos-latest` (Apple Silicon), and
   `macos-13` (Intel) runners in parallel.
2. Run the existing `build_exe.bat` / `build_macos.sh` scripts on
   each.
3. Zip the `.app` bundles so they survive the upload round-trip.
4. Publish a [GitHub Release](https://docs.github.com/en/repositories/releasing-projects-on-github)
   at `https://github.com/<you>/bill-generator/releases/tag/v4.5.2`
   with all three binaries attached and auto-generated release notes
   from your commit history.

End-users just go to the Releases page and download the file for
their OS — no build instructions, no Python install, nothing.

### Triggering a build without cutting a release

Need to smoke-test a build without publishing it? Go to **Actions →
Build and release → Run workflow** in the GitHub UI. The workflow
builds all three binaries and uploads them as workflow artifacts
(downloadable from the run page) without creating a public Release.

### Cost

Free for public repositories — GitHub gives unlimited Actions minutes
to public repos. For private repos, you get 2000 free minutes/month;
a full Windows + 2× macOS build takes ~15 minutes total, so the free
tier covers ~130 builds/month.

### Distributing through package managers

GitHub Releases is the canonical download source, but you'll get a
better install UX (and dodge some OS trust prompts) by also publishing
through platform package managers:

- **Windows** — submit to [winget-pkgs](https://github.com/microsoft/winget-pkgs)
  so users can `winget install BillGenerator`. Scaffolds, templates,
  and a step-by-step submission guide live in
  [`packaging/winget/`](packaging/winget/).
- **macOS** — publish a [Homebrew Cask](https://docs.brew.sh/Cask-Cookbook)
  so users can `brew install --cask bill-generator`. Cask template and
  tap-setup instructions live in [`packaging/homebrew/`](packaging/homebrew/).

Both are free, neither requires code signing, and both noticeably
improve the user's first-launch experience.

---

## Building locally (without GitHub Actions)

If you'd rather build on your own machine, the same scripts the CI
uses are right there in the repo. PyInstaller can't cross-compile, so
you can only build for the OS you're sitting on.

### macOS

Requirements: Python 3.11+ (`brew install python@3.11`) and Xcode
Command Line Tools (`xcode-select --install`).

```bash
./build_macos.sh
```

Output:
- `dist/BillGenerator_V4.5.1.app` — double-click to launch
- `dist/BillGenerator_V4.5.1` — CLI binary

The `.app` is **unsigned**. On first launch Gatekeeper will block it
— right-click → **Open** → confirm. To distribute without that
warning, sign and notarize the app (requires an Apple Developer
account, $99/year): see [Apple's notarization docs](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution).

### Windows

Requirements: Python 3 installed on the Windows machine.

```bat
build_exe.bat
```

Output: `dist\BillGenerator_V4.5.1.exe`.

### Linux

There's no Linux build script yet — PRs welcome. Copy
`build_macos.sh`, drop the `--windowed` flag, and run on the target
distribution as a starting point.

---

## Optional: Supabase sync

If you want an off-machine mirror of your data, bring your own Supabase
project:

1. Create a project at <https://supabase.com> and copy its **URL** and
   **anon key**.
2. In the app go to **Account Settings → Supabase** and paste both,
   then save.
3. Use **Upload All Data to Supabase** for a full sync, or the
   incremental sync for deltas.

The app refuses non-HTTPS URLs and obvious internal targets (localhost,
RFC1918) before uploading. Last-sync timestamps are written back to
`info.json` so the home page can show when you last synced.

---

## API reference

The app exposes a small JSON API for integrations or scripts. All
endpoints serve `127.0.0.1` by default with **no authentication** — see
[SECURITY.md](SECURITY.md) for the threat model.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/bill_items/<invoice_no>` | GET | Customer + line items + totals for an invoice |
| `/api/generate_upi_qr` | GET | Base64 SVG UPI QR. Params: `upi_id` (required), `am`, `pn`, `cu` |
| `/accounting/customer_summary/<customer_id>` | GET | Invoiced / paid / outgoing / balance snapshot |
| `/accounting/amount_to_words` | GET | Convert numeric amount to Indian rupee words |
| `/api/statements` | GET | Per-period statement summaries (date range or year/month) |
| `/api/statements/invoices` | GET | Paginated raw invoice export |
| `/analytics_event` | POST | Records front-end analytics events locally |

---

## Architecture

```
├── app.py                 # Flask routes, branding, backups, sync orchestration
├── api.py                 # JSON / UPI-QR endpoints
├── analytics.py           # Sales / customer aggregations
├── analytics_tracking.py  # Front-end event logging
├── supabase_upload.py     # Optional cloud mirror
├── migration.py           # Startup schema migration
├── desktop_launcher.py    # pywebview entrypoint for packaged builds
├── db/
│   ├── models.py          # SQLAlchemy models
│   ├── db_events.py       # ORM event hooks
│   └── info.example.json  # Settings template
├── migrations/            # Alembic migration scripts
├── templates/             # Jinja templates (Bootstrap-based)
├── static/                # CSS / JS / SVG / fonts (pre-built)
└── tests/                 # pytest suite
```

- **Backend:** Flask + SQLAlchemy + Flask-Migrate, served by waitress
- **Database:** SQLite (local file)
- **Frontend:** Jinja templates with Bootstrap, Chart.js for analytics
- **PDFs:** browser print-to-PDF (no headless Chrome required)

---

## Running the tests

```bash
pip install pytest
pytest -q
```

Each test spins up a temporary data directory, so the suite never
touches your real `info.json` or `app.db`.

---

## Security

This app assumes a **single-user, local-only** deployment. It ships
with no authentication, partial CSRF protection (bill forms only), and
no encryption at rest. The defaults bind to loopback so only your
machine can reach it. **Read [SECURITY.md](SECURITY.md) before exposing
it on a network.**

To report a vulnerability, open a private security advisory on GitHub
— please don't file public issues for security bugs.

---

## Contributing

Pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for
setup, test, and style guidelines. Areas where help is especially
appreciated:

- Global CSRF protection across all state-changing routes
- Pluggable currency formatting / internationalisation (today's number
  words are Indian-numbering-system only)
- Replacing the bundled brand assets with your own logo
- Linux / macOS packaging recipes alongside the existing Windows build
- Internationalisation of UI strings (currently English-only)

---

## License

MIT — see [LICENSE](LICENSE).
