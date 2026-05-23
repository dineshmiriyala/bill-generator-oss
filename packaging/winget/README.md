# winget submission

Once your end-users can run

```cmd
winget install BillGenerator
```

the install experience on Windows becomes one-line and trusted.
Submissions are PRs against [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).

## One-time prep

1. Decide your **PackageIdentifier**. The convention is
   `<Publisher>.<App>`, both PascalCase, no spaces. For this project a
   reasonable choice is `BillGenerator.BillGenerator`.
   - If you fork or rebrand, swap `BillGenerator` for your own identifier.
2. Install `wingetcreate` on a Windows machine (you'll need it once
   per release):

   ```cmd
   winget install Microsoft.WingetCreate
   ```

## Per-release submission flow (after your first GitHub Release exists)

On a Windows machine:

```cmd
wingetcreate new https://github.com/<you>/bill-generator/releases/download/v4.5.2/BillGenerator_V4.5.2.exe
```

`wingetcreate` interactively walks you through:

- PackageIdentifier (use `BillGenerator.BillGenerator` or your fork's slug)
- Version (`4.5.2`)
- Publisher, Author, License, Description (copy from this repo)
- Architecture (`x64`)
- Computes the SHA256 automatically from the URL
- Validates the manifest
- Opens a PR against `microsoft/winget-pkgs` on your behalf

Microsoft's automated bots validate the submission in ~10 minutes. A
human reviewer merges it within a day or two. Once merged it goes live
in the public winget catalog.

## Subsequent updates

Once your initial submission is merged, future versions are even simpler:

```cmd
wingetcreate update BillGenerator.BillGenerator --version 4.5.3 --urls https://github.com/<you>/bill-generator/releases/download/v4.5.3/BillGenerator_V4.5.3.exe --submit
```

The `--submit` flag opens the PR automatically.

## Templates

Manifest templates with placeholders are in [`templates/`](templates/)
for reference if you'd rather hand-write the YAML instead of using
`wingetcreate`. Replace every `__PLACEHOLDER__` value before submitting.

## Common gotchas

- **`InstallerSwitches.Silent`** for the Bill Generator `.exe`:
  PyInstaller-built executables don't accept install switches. Leave
  the `Silent` / `SilentWithProgress` fields blank — the manifest is
  for a portable / standalone executable, not an installer.
- **`InstallerType`**: set to `portable` since this is a single-file
  `.exe` with no installer wrapper.
- **`Publisher` ↔ identifier mismatch**: the `Publisher` field in the
  locale manifest must match the first half of `PackageIdentifier`
  (e.g. `BillGenerator` ↔ `BillGenerator.BillGenerator`).
