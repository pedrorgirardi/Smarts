#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $(basename "$0") <source-icon>"
  echo "Example: $(basename "$0") '/Applications/Sublime Text.app/Contents/Resources/Sublime Text.icns'"
  exit 1
fi

if ! command -v sips >/dev/null 2>&1; then
  echo "Error: 'sips' is required but not found."
  exit 1
fi

source_icon="$1"
if [[ ! -f "$source_icon" ]]; then
  echo "Error: source icon not found: $source_icon"
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
assets_dir="$(cd "$script_dir/.." && pwd)/assets"
output_png="$assets_dir/companion-icon-1024.png"
output_icns="$assets_dir/companion.icns"
tmp_png="$(mktemp /tmp/companion-icon.XXXXXX.png)"

cleanup() {
  rm -f "$tmp_png"
}
trap cleanup EXIT

# Normalize any supported image input to PNG, resize to 1024x1024,
# then generate both companion icon assets from that canonical image.
sips -s format png "$source_icon" --out "$tmp_png" >/dev/null
sips -z 1024 1024 "$tmp_png" --out "$output_png" >/dev/null
sips -s format icns "$output_png" --out "$output_icns" >/dev/null

echo "Updated:"
echo "  $output_png"
echo "  $output_icns"
