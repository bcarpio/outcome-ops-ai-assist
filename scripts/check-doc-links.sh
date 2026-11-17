#!/usr/bin/env bash
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# Fail if any relative markdown link points at a repo file that does not exist.
#
# validate-docs only checks file SIZES for embedding; nothing checked that the
# links between docs actually resolve, so five links rotted silently before a
# human caught them. This makes that class of regression a CI failure instead.
#
# Scope: relative *.md links in README.md + docs/**.md. External (http/https),
# mailto:, same-file #anchors, and non-.md targets are skipped. Anchor targets
# are existence-checked at the file level (the #fragment is stripped); it does
# not yet verify the heading exists.
set -u

broken=0
for f in README.md docs/*.md docs/adr/*.md; do
  [ -f "$f" ] || continue
  dir=$(dirname "$f")
  while IFS= read -r target; do
    case "$target" in
      http://* | https://* | mailto:*) continue ;;
    esac
    path=${target%%#*}                       # strip #anchor
    [ -z "$path" ] && continue               # pure same-file anchor
    case "$path" in *.md) : ;; *) continue ;; esac
    case "$path" in
      /*) resolved=".$path" ;;               # repo-absolute
      *) resolved="$dir/$path" ;;            # relative to the file's dir
    esac
    if [ ! -f "$resolved" ]; then
      printf '  BROKEN  %s -> %s\n' "$f" "$target"
      broken=$((broken + 1))
    fi
  done < <(grep -oE '\]\([^) ]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//')
done

if [ "$broken" -gt 0 ]; then
  echo "Doc link check FAILED: $broken broken markdown link(s) above."
  exit 1
fi
echo "Doc link check: all relative markdown links resolve."
