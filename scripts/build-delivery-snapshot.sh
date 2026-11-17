#!/usr/bin/env bash
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# Build a HISTORY-FREE delivery snapshot of the platform for a customer.
#
# WHY: customer names are scrubbed from the working tree, but they survive in
# git HISTORY (past commits). Delivering a history-free snapshot guarantees a
# customer never receives that history -- they get only the current, scrubbed
# tree. This is the delivery mechanism that makes a full `git filter-repo`
# history rewrite optional for client protection.
#
# Modes:
#   (default)            -> writes a tarball to $OUT_DIR (no .git, no history)
#   CUSTOMER_REMOTE set  -> pushes ONE squashed (orphan) commit to the
#                           customer-owned repo's $PUSH_BRANCH (no ancestry)
#
# Env vars (all optional):
#   REF             git ref to deliver          (default: HEAD)
#   OUT_DIR         tarball output dir          (default: dist)
#   CUSTOMER_REMOTE customer repo git URL       (enables push mode)
#   PUSH_BRANCH     branch to push to           (default: main)
set -euo pipefail

REF="${REF:-HEAD}"
OUT_DIR="${OUT_DIR:-dist}"
CUSTOMER_REMOTE="${CUSTOMER_REMOTE:-}"
PUSH_BRANCH="${PUSH_BRANCH:-main}"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

VER="$(git describe --tags --always "$REF" 2>/dev/null || git rev-parse --short "$REF")"

# Safety net: never ship a scrubbed name. The patterns come from a
# maintainer-local, gitignored file (.delivery-forbidden, one regex per line) or
# $DELIVERY_FORBIDDEN -- so the names themselves never live in the committed or
# delivered tree (this script stays generic).
FORBIDDEN="${DELIVERY_FORBIDDEN:-}"
if [ -z "$FORBIDDEN" ] && [ -f "$ROOT/.delivery-forbidden" ]; then
  FORBIDDEN="$(grep -vE '^\s*(#|$)' "$ROOT/.delivery-forbidden" | tr '\n' '|' | sed 's/|$//')"
fi
if [ -n "$FORBIDDEN" ]; then
  if git grep -niE "$FORBIDDEN" "$REF" -- . >/dev/null 2>&1; then
    echo "ABORT: a forbidden name is present in the tree at $REF -- do NOT deliver." >&2
    git grep -niE "$FORBIDDEN" "$REF" -- . | head >&2
    exit 1
  fi
  echo "Name-scrub guard: passed (no forbidden patterns in $REF)."
else
  echo "NOTE: no .delivery-forbidden or \$DELIVERY_FORBIDDEN set -- name-scrub guard is INACTIVE." >&2
fi

if [ -n "$CUSTOMER_REMOTE" ]; then
  # --- Push mode: single orphan commit to the customer's repo ---
  WT="$ROOT/.delivery-worktree"
  git worktree remove --force "$WT" 2>/dev/null || true
  git worktree add --detach "$WT" "$REF" >/dev/null
  (
    cd "$WT"
    git checkout --orphan delivery >/dev/null 2>&1
    git add -A
    git commit -q -m "OutcomeOps AI Assist ${VER}"
    echo "Pushing history-free snapshot ${VER} -> ${CUSTOMER_REMOTE} (${PUSH_BRANCH}) ..."
    git push "$CUSTOMER_REMOTE" "delivery:${PUSH_BRANCH}"
  )
  git worktree remove --force "$WT"
  echo "Delivered ${VER} to ${CUSTOMER_REMOTE} as a single history-free commit on ${PUSH_BRANCH}."
else
  # --- Archive mode: history-free tarball ---
  mkdir -p "$OUT_DIR"
  OUT="$OUT_DIR/outcome-ops-ai-assist-${VER}.tar.gz"
  git archive --format=tar.gz --prefix="outcome-ops-ai-assist-${VER}/" -o "$OUT" "$REF"
  echo "History-free delivery snapshot: $OUT"
  echo "  (no .git, no history -- the customer inits their own repo from these files)"
  echo "To push directly into a customer-owned repo instead:"
  echo "  make deliver CUSTOMER_REMOTE=<git-url> [PUSH_BRANCH=main] [REF=origin/main]"
fi
