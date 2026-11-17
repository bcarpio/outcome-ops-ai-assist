# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Shared token-sizing heuristics for the code-map summarization pipeline.

Two consumers use these constants + estimator: `process-repo-summary`
(upstream splitter — decides how many SQS shard messages to send per
code unit) and `process-batch-summary` (downstream consumer — validates
that the shard fits in one Bedrock call before dispatching).

Keeping the heuristic in ONE module prevents the two Lambdas from
drifting on the "does this fit" question. If the split threshold and
the consumer's ceiling ever diverged, the splitter would send messages
the consumer would then internally re-split, and we'd be back to the
15-minute timeout that this migration exists to fix.

## Constants

- ``TARGET_SHARD_TOKENS`` — soft cap the upstream splitter aims for.
  Default 120_000; env var ``CODE_MAP_TARGET_SHARD_TOKENS`` overrides.
- ``MAX_PROMPT_TOKENS`` — hard ceiling for one Bedrock summarize call.
  Default 150_000; env var ``CODE_MAP_MAX_PROMPT_TOKENS`` overrides.
  Matches the historical value used in-handler at
  ``process-batch-summary/handler.py`` before this extraction.

Target < Max leaves ~30k tokens of headroom for the prompt template +
merge instructions + output tokens. If a shard estimate is at
TARGET_SHARD_TOKENS + one-file-slop, it still fits under the ceiling.

## Estimator

``estimate_tokens(text)`` uses the same conservative ``len(text) // 3``
heuristic that lived in-handler. Sonnet's tokenizer averages closer to
3.5 chars/token for code; the 3-divisor over-estimates slightly, which
means shards err on the side of "smaller than they need to be" rather
than "too big to summarize." That's the right direction for a fix
designed to stop timeouts.

``estimate_batch_tokens(file_contents)`` sums per-file token estimates
plus a fixed overhead for the surrounding prompt template (delimiters,
"File: {path}" preamble, code fences). Overhead is 30 tokens per file
plus 500 tokens of static template.

Not planned as a follow-up: swapping to a real Bedrock tokenizer. Sonnet's
tokenizer differs from tiktoken; measuring real usage from CloudWatch
would be a better signal than any local library. Revisit if the
char/3 heuristic proves over- or under-sharding in practice.
"""

from __future__ import annotations

import os
from typing import Iterable, Tuple


# Hard ceiling for one Bedrock call. Overridable via env for tuning
# without a redeploy of the code.
MAX_PROMPT_TOKENS: int = int(os.environ.get("CODE_MAP_MAX_PROMPT_TOKENS", "150000"))

# Target the upstream splitter aims for. Under the ceiling by enough to
# absorb the template overhead + the last file that pushes a shard over
# the target during first-fit packing.
TARGET_SHARD_TOKENS: int = int(os.environ.get("CODE_MAP_TARGET_SHARD_TOKENS", "120000"))


# Prompt-template overhead per file: "File: {path}\n```\n\n```\n" plus
# occasional chunk-info suffix. Conservative estimate; token cost of
# path itself is included in the content estimate.
_PER_FILE_OVERHEAD_TOKENS = 30

# Fixed overhead for the surrounding prompt template (system prompt,
# instructions, formatting scaffolding).
_TEMPLATE_OVERHEAD_TOKENS = 500


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string.

    Uses len(text) // 3 -- code averages ~3.5 chars/token; the 3-divisor
    is conservative and matches the pre-existing in-handler heuristic
    that shipped in ``process-batch-summary`` before this extraction.
    """
    return len(text) // 3


def estimate_batch_tokens(
    file_contents: Iterable[Tuple[str, str]],
) -> int:
    """Estimate token cost of summarizing a batch of files together.

    Args:
        file_contents: Iterable of (path, content) pairs. Path is the
            file path used in the prompt preamble; content is the file
            body that dominates the estimate.

    Returns:
        Estimated total token cost including per-file preamble
        (~30 tokens each) plus a fixed template overhead (~500 tokens).
    """
    total = _TEMPLATE_OVERHEAD_TOKENS
    for path, content in file_contents:
        total += _PER_FILE_OVERHEAD_TOKENS
        total += estimate_tokens(path)
        total += estimate_tokens(content)
    return total


def estimate_tokens_from_size(size_bytes: int) -> int:
    """Estimate token cost from a file's byte size (GitHub tree API's
    `size` field). For ASCII source code, bytes ~= characters, so the
    same char/3 heuristic applies. UTF-8 code may be slightly larger
    than the token estimate suggests; the char/3 divisor is conservative
    enough to absorb the drift."""
    return size_bytes // 3


def pack_files_into_shards(
    file_sizes: Iterable[Tuple[str, int]],
    target_tokens_per_shard: int,
) -> list[list[str]]:
    """First-fit-decreasing packing: sort files by estimated tokens
    descending, then assign each to the first shard that has room.

    Deterministic (sort is stable) so a re-run against the same input
    produces the same shard boundaries.

    Args:
        file_sizes: Iterable of (path, size_in_bytes) pairs. Size may be
            0 (unknown); those files are packed last after the
            larger-first pass finishes.
        target_tokens_per_shard: Soft cap per shard. Callers usually
            pass ``TARGET_SHARD_TOKENS``. First-fit-decreasing may
            slightly overflow one shard when a single large file
            exceeds the target -- that file gets its own shard.

    Returns:
        List of shards; each shard is a list of file paths. Empty
        input returns an empty list (not a single empty shard).
    """
    items = list(file_sizes)
    if not items:
        return []
    # Sort by size descending so the packer places big files first.
    items.sort(key=lambda pair: pair[1], reverse=True)

    shards: list[list[str]] = []
    shard_token_totals: list[int] = []

    for path, size in items:
        cost = estimate_tokens_from_size(size) + _PER_FILE_OVERHEAD_TOKENS
        placed = False
        for i, total in enumerate(shard_token_totals):
            if total + cost <= target_tokens_per_shard:
                shards[i].append(path)
                shard_token_totals[i] = total + cost
                placed = True
                break
        if not placed:
            # Either no existing shard has room, or the file alone
            # exceeds the target (single huge file). Either way, new
            # shard.
            shards.append([path])
            shard_token_totals.append(_TEMPLATE_OVERHEAD_TOKENS + cost)
    return shards
