# Packaging

Scaffolds and instructions for distributing Bill Generator through
platform package managers, in addition to direct GitHub Release
downloads.

The two main reasons to do this:

1. **Better install UX.** Users prefer `winget install ...` or
   `brew install --cask ...` over downloading a binary and clicking
   past OS warnings.
2. **Better trust signal.** Apps installed via a package manager run
   with elevated trust on the host OS, which sidesteps some of the
   "from unknown publisher" prompts.

Neither replaces real code signing, but both meaningfully improve the
end-user experience.

| Platform | Subdir | Effort | Cost |
|----------|--------|--------|------|
| Windows  | [`winget/`](winget/) | ~1 hour first submission, ~5 min per release | Free |
| macOS    | [`homebrew/`](homebrew/) | ~1 hour first setup, automated after | Free |

See each subdirectory's README for step-by-step submission instructions.
