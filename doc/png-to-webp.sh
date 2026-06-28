#!/usr/bin/env bash
#
# Convert every PNG in the doc folder to WebP using the system ImageMagick
# (installed via Homebrew: `brew install imagemagick`).
#
# By default it processes the doc folder this script lives in; pass one or more
# directories to convert those instead. Originals are kept — a foo.png becomes
# a sibling foo.webp. Existing .webp files are skipped unless --force is given.
#
#   ./png-to-webp.sh                # convert PNGs in this script's folder
#   ./png-to-webp.sh path/to/dir    # convert PNGs in another folder
#   ./png-to-webp.sh --force        # re-encode even if the .webp already exists
#   ./png-to-webp.sh -q 90          # set WebP quality (default 82)
#
set -euo pipefail

QUALITY=82
FORCE=0
DIRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -q|--quality) QUALITY="${2:?-q needs a value}"; shift 2 ;;
    -f|--force)   FORCE=1; shift ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  DIRS+=("$1"); shift ;;
  esac
done

# ImageMagick 7 ships `magick`; older Homebrew kegs only have `convert`.
if command -v magick >/dev/null 2>&1; then
  CONVERT=(magick)
elif command -v convert >/dev/null 2>&1; then
  CONVERT=(convert)
else
  echo "error: ImageMagick not found. Install it with: brew install imagemagick" >&2
  exit 1
fi

# Default to the directory this script lives in (the doc folder).
if [[ ${#DIRS[@]} -eq 0 ]]; then
  DIRS=("$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")
fi

shopt -s nullglob nocaseglob
converted=0
skipped=0
for dir in "${DIRS[@]}"; do
  if [[ ! -d "$dir" ]]; then
    echo "skip: not a directory: $dir" >&2
    continue
  fi
  for png in "$dir"/*.png; do
    webp="${png%.*}.webp"
    if [[ $FORCE -eq 0 && -f "$webp" && "$webp" -nt "$png" ]]; then
      echo "skip (up to date): ${webp##*/}"
      skipped=$((skipped + 1))
      continue
    fi
    "${CONVERT[@]}" "$png" -quality "$QUALITY" "$webp"
    before=$(wc -c <"$png" | tr -d ' ')
    after=$(wc -c <"$webp" | tr -d ' ')
    printf 'converted: %s  (%sB -> %sB)\n' "${webp##*/}" "$before" "$after"
    converted=$((converted + 1))
  done
done

echo "done: $converted converted, $skipped skipped (quality $QUALITY)"
