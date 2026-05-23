# Homebrew Cask

Once set up, Mac users can install with:

```bash
brew install --cask bill-generator
```

There are two routes — start with the **custom tap** route since
homebrew-cask itself only accepts apps with a real user base.

## Route 1 — Custom tap (recommended for new projects)

A "tap" is a separate GitHub repo named `homebrew-<anything>` that
Homebrew can install casks from.

### One-time setup

1. Create a new GitHub repo: `homebrew-bill-generator`
   (must start with `homebrew-`).
2. Inside it, create the directory `Casks/`.
3. Copy [`bill-generator.rb`](bill-generator.rb) from this directory
   into `Casks/bill-generator.rb` in the tap repo.
4. Fill in the `__PLACEHOLDERS__` (see "Per-release updates" below).
5. Commit and push.

Users then install with:

```bash
brew tap <your-gh-user>/bill-generator
brew install --cask bill-generator
```

Or in one line:

```bash
brew install --cask <your-gh-user>/bill-generator/bill-generator
```

### Per-release updates

Every time you cut a new GitHub Release, update `Casks/bill-generator.rb`
in the tap repo:

1. Bump the `version` field.
2. Update the two `sha256` values. Compute them with:

   ```bash
   curl -sL https://github.com/<you>/bill-generator/releases/download/v4.5.2/BillGenerator_V4.5.2-macos-arm64.zip | shasum -a 256
   curl -sL https://github.com/<you>/bill-generator/releases/download/v4.5.2/BillGenerator_V4.5.2-macos-intel.zip | shasum -a 256
   ```

3. Commit, push. Users `brew upgrade --cask bill-generator` to update.

You can automate this with a small GitHub Action in the tap repo that
listens for release webhooks from this repo and opens a PR — see the
"Auto-bump" section in the [cask file](bill-generator.rb).

## Route 2 — Official homebrew-cask

Apply once your project has visible user adoption (typically 30+
GitHub stars / a real release history). The cask file format is
identical to Route 1; the submission is a PR to
[Homebrew/homebrew-cask](https://github.com/Homebrew/homebrew-cask).
See their [Acceptable Casks](https://github.com/Homebrew/homebrew-cask/blob/master/CONTRIBUTING.md#acceptable-casks)
policy for current requirements.
