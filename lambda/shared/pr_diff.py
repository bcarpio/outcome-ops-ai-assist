"""
Client-side unified-diff generator.

GitHub exposes a server-side diff endpoint (``Accept:
application/vnd.github.v3.diff`` on a PR URL). Azure DevOps does not
have an equivalent — its REST API exposes base and head file
contents via the items API but leaves diff computation to the client.

The PR-check pipeline's retrieval-heavy handlers (ADR_COMPLIANCE,
ARCHITECTURAL_DUPLICATION) work off unified-diff strings. To keep the
handlers provider-agnostic below the fetch layer, this module produces
the same shape of diff string from base + head bytes as GitHub would
have served up.

Design decisions:

- **Bytes in, str out.** ``shared.source_fetch.fetch_file_at_sha``
  returns bytes because file contents may be binary. This module owns
  the decode step so each handler doesn't reimplement it.
- **UTF-8 with errors="replace"** for text decoding. A single stray
  byte should not fail the whole PR-check turn.
- **Binary detection heuristic.** A NUL byte in the first 1024 bytes
  of either side triggers a ``(binary file changed)`` stub rather
  than an attempted decode. Matches GitHub's diff output for binaries.
- **Added / deleted file support.** ``base_content=None`` means the
  file did not exist in the base commit (added). ``head_content=None``
  means it does not exist in head (deleted). Both cases produce a
  legal unified-diff block that the downstream LLM analyses can parse.
"""

import difflib
import logging
from typing import Optional


logger = logging.getLogger(__name__)


_BINARY_SNIFF_BYTES = 1024


def _looks_binary(content: Optional[bytes]) -> bool:
    if content is None:
        return False
    sniff = content[:_BINARY_SNIFF_BYTES]
    return b"\x00" in sniff


def _decode(content: Optional[bytes]) -> str:
    if content is None:
        return ""
    return content.decode("utf-8", errors="replace")


def compute_file_diff(
    base_content: Optional[bytes],
    head_content: Optional[bytes],
    filename: str,
    context_lines: int = 3,
) -> str:
    """Return a unified-diff string in the GitHub ``diff --git`` shape.

    Args:
        base_content: File bytes at the PR base commit, or ``None`` if
            the file did not exist there (i.e. the PR adds this file).
        head_content: File bytes at the PR head commit, or ``None`` if
            the file has been deleted in the PR.
        filename: Repo-relative path to embed in the diff header. Both
            "a/" and "b/" sides use the same filename per Git convention.
        context_lines: Number of unchanged lines of context around each
            hunk. Matches ``git diff -U{n}``; default 3.

    Returns:
        A unified-diff string. Contains the ``diff --git`` header on
        every non-empty result so ADR/duplication handlers'
        section-extractors can locate per-file boundaries.
        Returns the empty string only when base == head AND both are
        non-None (unchanged file — no diff).
    """
    if base_content is None and head_content is None:
        # Nothing to diff — file mentioned in changed_files but no
        # content on either side. Return an empty header so downstream
        # code doesn't crash.
        return f"diff --git a/{filename} b/{filename}\n(no content on either side)\n"

    if _looks_binary(base_content) or _looks_binary(head_content):
        return (
            f"diff --git a/{filename} b/{filename}\n"
            f"(binary file changed)\n"
        )

    base_text = _decode(base_content)
    head_text = _decode(head_content)

    if base_content is not None and head_content is not None and base_text == head_text:
        # Same content -- not really changed. Return empty so callers
        # can skip cleanly.
        return ""

    base_lines = base_text.splitlines(keepends=True)
    head_lines = head_text.splitlines(keepends=True)

    # Git-metadata prefix. Downstream consumers grep the diff string for
    # markers like ``"new file mode"`` (see
    # lambda/process-pr-check/check_handlers/license_compliance.py:is_new_file)
    # -- GitHub's server-side diff includes these lines and any diff we
    # produce needs to match. Live-burned 2026-07-01: without the
    # ``new file mode`` line, LICENSE_COMPLIANCE mis-classified an
    # added file as "existing" and skipped the copyright-header check.
    if base_content is None:
        base_label = "/dev/null"
        head_label = f"b/{filename}"
        git_meta = "new file mode 100644\n"
    elif head_content is None:
        base_label = f"a/{filename}"
        head_label = "/dev/null"
        git_meta = "deleted file mode 100644\n"
    else:
        base_label = f"a/{filename}"
        head_label = f"b/{filename}"
        git_meta = ""

    diff_iter = difflib.unified_diff(
        base_lines,
        head_lines,
        fromfile=base_label,
        tofile=head_label,
        n=context_lines,
        lineterm="",
    )

    header = f"diff --git a/{filename} b/{filename}\n{git_meta}"
    body = "\n".join(diff_iter)
    if body and not body.endswith("\n"):
        body += "\n"
    return header + body
