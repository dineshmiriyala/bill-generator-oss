# Homebrew Cask formula for Bill Generator.
#
# Copy this file to your tap repo at Casks/bill-generator.rb,
# then replace every __PLACEHOLDER__ value before committing.
#
# To compute SHA256 values for a given release:
#   curl -sL <download-url> | shasum -a 256
cask "bill-generator" do
  arch arm: "arm64", intel: "intel"

  version "__VERSION__"
  sha256 arm:   "__SHA256_ARM64__",
         intel: "__SHA256_INTEL__"

  url "https://github.com/__GH_OWNER__/__GH_REPO__/releases/download/v#{version}/BillGenerator_V#{version}-macos-#{arch}.zip",
      verified: "github.com/__GH_OWNER__/__GH_REPO__/"
  name "Bill Generator"
  desc "Local-first invoicing and accounting app for small businesses"
  homepage "https://github.com/__GH_OWNER__/__GH_REPO__"

  app "BillGenerator_V#{version}.app"

  zap trash: [
    "~/Library/Application Support/Bill Generator",
    "~/Library/Saved Application State/Bill Generator.savedState",
  ]
end

# ----------------------------------------------------------------------------
# Auto-bump tip
#
# To automate version + sha256 updates when new GitHub Releases are
# cut, add this workflow to your tap repo as
# .github/workflows/bump-cask.yml. It listens for the upstream
# `release` event and opens a PR with the updated cask file.
#
# name: Bump cask on upstream release
# on:
#   repository_dispatch:
#     types: [bill-generator-release]
# jobs:
#   bump:
#     runs-on: macos-latest
#     steps:
#       - uses: actions/checkout@v4
#       - run: brew bump-cask-pr bill-generator --version "${{ github.event.client_payload.version }}" --no-browse
#
# Then in this repo's release.yml, add a final step:
#
#   - name: Trigger cask bump
#     if: startsWith(github.ref, 'refs/tags/v')
#     uses: peter-evans/repository-dispatch@v3
#     with:
#       token: ${{ secrets.TAP_REPO_PAT }}
#       repository: __GH_OWNER__/homebrew-bill-generator
#       event-type: bill-generator-release
#       client-payload: '{"version":"${{ github.ref_name }}"}'
# ----------------------------------------------------------------------------
