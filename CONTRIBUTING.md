# Contributing to Bill Generator

Thanks for your interest in contributing. The repo is small enough that
process can stay light, but please read this once before opening a PR.

## Setup

```bash
git clone <your-fork>
cd bill-generator
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp db/info.example.json db/info.json   # or let onboarding create one
python app.py                           # http://127.0.0.1:42069
```

The first run will either pick up `db/info.json` or walk you through
the onboarding wizard to create one.

## Running tests

```bash
pip install pytest
pytest -q
```

Tests are isolated — each test runs against a temporary data directory
so they don't touch your real `info.json` or `app.db`.

## What we'd love help with

- Adding global CSRF protection (Flask-WTF) across all state-changing
  routes — see SECURITY.md for the current gap.
- Pluggable currency formatting / number-to-words. Right now the
  Indian numbering (Lakh / Crore) is wired into `format_inr()` and
  `rupees_to_words()` in `app.py`.
- Replacing the bundled brand assets (`static/img/brand-water-mark-*.svg`,
  `static/img/app_icon.svg`) with your own branding.
- Internationalisation. UI strings are currently hardcoded English.
- Better packaging (Linux / macOS) — current build script targets
  Windows via PyInstaller.

## Coding style

- Python 3.11+, type hints where they clarify intent.
- Use `Decimal` for any currency math; never `float`.
- Templates: use Jinja autoescape; avoid `|safe` unless the value is
  literally hardcoded HTML.
- Don't introduce new raw SQL — stick to SQLAlchemy ORM.
- New env vars: prefix with `BG_` (e.g. `BG_BIND_HOST`,
  `BG_INVOICE_PREFIX`).

## Commit messages

One-line summary in the imperative mood, plus a short body if the
change isn't obvious. Reference an issue if there is one.

## Pull requests

1. Branch off `main`.
2. Run the test suite locally.
3. Update README / SECURITY / CONTRIBUTING if your change affects
   behaviour, threat model, or the dev workflow.
4. Squash trivia into a single commit before requesting review.

## Reporting bugs

Open an issue with: what you expected, what happened, OS + Python
version, and the smallest set of steps to reproduce. If the bug is a
security issue, please follow SECURITY.md instead.
