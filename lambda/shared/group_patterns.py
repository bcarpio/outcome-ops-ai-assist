# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Azure AD group naming-convention matching (ADR-048 v1.1).

Pure, side-effect-free. A deployment configures a list of case-insensitive glob
patterns (``azure_ad_group_patterns``, e.g. ``["outcomeops-*", "*-outcomeops-*"]``)
that scope which groups are ELIGIBLE and VISIBLE for binding to a workspace --
the OutcomeOps-side analog of Okta's ``aws-*`` group-push convention.

The patterns are a visibility/eligibility filter only; they are never stored as
a binding (bindings stay explicit and per-group, ADR-048 v1.1 §3). They are used
to (a) decide which of a user's claim values are "ours" worth showing/matching by
name, (b) bound the Graph fallback query, and (c) filter the admin's picker.

Empty pattern list ⇒ NO restriction (every group is eligible) -- the v1.0
back-compat behavior, so a deploy that never sets the tfvar is unchanged.
"""

import re
from fnmatch import fnmatch
from typing import Dict, Iterable, List, Optional, Sequence

# Entra object IDs are GUIDs. A claim value that matches this is an object ID;
# anything else (that got emitted) is an on-prem group name (sAMAccountName).
_OBJECT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_object_id(value: str) -> bool:
    """True if ``value`` is an Entra group object ID (GUID), else it's a name."""
    return bool(value) and bool(_OBJECT_ID_RE.match(value.strip()))


def matches(name: str, patterns: Sequence[str]) -> bool:
    """Case-insensitive glob match of ``name`` against ANY pattern.

    Empty ``patterns`` ⇒ True (no restriction). Object-ID values never match a
    name-style pattern unless a pattern is a bare ``*`` (explicit allow-all).
    """
    if not patterns:
        return True
    if not name:
        return False
    low = name.strip().lower()
    return any(fnmatch(low, p.strip().lower()) for p in patterns if p)


def filter_names(
    names: Iterable[str],
    patterns: Sequence[str],
) -> List[str]:
    """Keep the claim string values that match the patterns.

    GUIDs are kept only when the patterns don't restrict names away from them:
    an object ID passes iff ``patterns`` is empty or contains an allow-all
    ``*`` (a GUID can't match ``outcomeops-*``). Callers that need to resolve
    GUIDs by pattern do so via Graph, not here.
    """
    out: List[str] = []
    for n in names:
        if not n:
            continue
        if looks_like_object_id(n):
            # a GUID only survives a name filter if there's no real restriction
            if not patterns or any(p.strip() == "*" for p in patterns):
                out.append(n)
            continue
        if matches(n, patterns):
            out.append(n)
    return out


def filter_groups(
    groups: Iterable[Dict],
    patterns: Sequence[str],
    name_key: str = "display_name",
) -> List[Dict]:
    """Keep the group dicts whose ``name_key`` matches the patterns."""
    return [g for g in groups if matches(g.get(name_key, ""), patterns)]


def graph_prefix_filters(patterns: Sequence[str]) -> Optional[List[str]]:
    """Translate leading-prefix globs to Graph ``startswith`` filter fragments.

    Only patterns of the form ``prefix*`` (a single trailing star) become a
    server-side ``startswith(displayName,'prefix')`` fragment -- the cheap,
    indexed Graph filter. Patterns with a leading/embedded star (``*-outcomeops-*``)
    can't be a startswith and are applied client-side via ``filter_groups`` after
    a broader fetch. Returns None when no pattern is a clean prefix (caller then
    fetches by page + filters locally), or an empty list only if ``patterns`` is
    empty (caller decides its own default).
    """
    if not patterns:
        return []
    prefixes: List[str] = []
    for p in patterns:
        p = p.strip()
        if p and p.endswith("*") and "*" not in p[:-1] and "?" not in p[:-1] and "[" not in p[:-1]:
            prefixes.append(p[:-1])
    return prefixes or None
