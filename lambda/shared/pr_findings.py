# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""shared.pr_findings -- structured PR-review findings for inline comments.

A check handler returns, alongside its ``status`` / ``message`` / ``details``,
a ``findings`` list: one :class:`Finding` per issue it can anchor to a specific
file + line. ``process-pr-check`` posts each Finding as a line-level (inline)
review comment on the PR/MR -- GitHub review comment, GitLab discussion with a
diff ``position``, or an Azure DevOps thread with ``threadContext`` -- upserted
and stale-resolved per (check, file, line). Findings are additive: the per-check
``<details>`` summary comment is unchanged, so a finding with no safe line anchor
simply stays in the summary and is not inline-posted.

Contract (grounded in ADR-031 line-citation + ADR-032 list-completeness):

- ``line`` is the 1-based line on the post-change (RIGHT) side of the diff that
  the finding refers to. Anchor to the actual changed / offending line the check
  observed -- never a guessed or averaged line. A check that only knows the file
  (not a line) should anchor to the file's first changed line in the PR diff, or
  omit the Finding and let the summary carry it.
- One Finding == one concrete issue at one location. Do NOT encode an
  LLM-computed aggregate ("3 issues found") in ``message`` -- that is the
  ADR-031/ADR-032 anti-aggregate rule; enumerate concrete findings instead.
- ``enclosing_symbol`` is optional context. Leave it ``None`` unless it was
  verified via ``shared.symbol_graph_reader.symbol_at_line`` -- ADR-031 forbids
  guessing the enclosing symbol name when it cannot be resolved.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """One line-anchored PR-review finding (see module docstring for the contract)."""

    path: str = Field(..., description="Repo-relative file path, as it appears in the diff")
    line: int = Field(..., gt=0, description="1-based line on `side`")
    side: Literal["RIGHT", "LEFT"] = "RIGHT"
    severity: Literal["info", "warning", "error"] = "warning"
    message: str = Field(..., description="Human-readable finding text; no aggregate counts")
    enclosing_symbol: Optional[str] = Field(
        None, description="Verified enclosing symbol (symbol_at_line) or None; never guessed"
    )

    class Config:
        populate_by_name = True
