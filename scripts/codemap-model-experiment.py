#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Compare Haiku vs Sonnet on the code-map generation prompt for a Lambda.

Reuses the exact `handler-group` prompt template from
process-batch-summary/handler.py:_build_batch_prompt so the comparison
is apples-to-apples with what production uses.

Usage:
    python scripts/codemap-model-experiment.py mcp-server process-batch-summary process-documents

Output:
    experiment/<lambda>.sonnet.md  -- code-maps/advanced tier
    experiment/<lambda>.haiku.md   -- code-maps/basic tier
    stdout                         -- token counts + $ cost per run

Requires:
    - AWS credentials that can Invoke code-maps profile ARNs in us-west-2
    - venv activated (source lambda/.venv/bin/activate)

Spend attribution:
    Attributes under bedrock-workload-code-maps in Cost Explorer since
    we invoke via shared.model_client.invoke(workload="code-maps", ...).
"""
import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda"))

# Fetch the code-maps profile ARNs from SSM up front so shared.model_client's
# tier -> env-var lookup resolves. In the Lambda runtime these come from the
# terraform-wired env vars; running locally we grab them from SSM. Bootstrap
# the env BEFORE importing shared.model_client (module-init reads the env).
import boto3

_SSM = boto3.client("ssm", region_name="us-west-2")


def _prime_env():
    mapping = {
        "BEDROCK_CODE_MAPS_ADVANCED_PROFILE_ARN":
            "/prd/outcome-ops-ai-assist/bedrock/code-maps/sonnet-profile-arn",
        "BEDROCK_CODE_MAPS_BASIC_PROFILE_ARN":
            "/prd/outcome-ops-ai-assist/bedrock/code-maps/haiku-profile-arn",
    }
    for env_var, ssm_param in mapping.items():
        if env_var in os.environ:
            continue
        try:
            v = _SSM.get_parameter(Name=ssm_param)["Parameter"]["Value"]
            os.environ[env_var] = v
        except Exception as e:
            print(f"WARN: could not fetch {ssm_param}: {e}", file=sys.stderr)


_prime_env()

from shared.model_client import invoke  # noqa: E402
from shared.prompts import CODE_MAP_LITERAL_DISCIPLINE  # noqa: E402

REPO_NAME = "outcome-ops-ai-assist"

# Verbatim copy of the "handler-group" template from
# process-batch-summary/handler.py:_build_batch_prompt (line ~483). Keeping
# it inline instead of importing so this script is drop-in runnable without
# fighting the Lambda module's boto3 client-init graph.
HANDLER_GROUP_TEMPLATE = """Write a CONCISE implementation overview of the {group_name} Lambda handler for {repo}. Target ~3000 words. A reader uses this summary as an INDEX to navigate the source -- they will read source for exact values. Do not turn this into a documentation rewrite.

Discipline:
- Group functions by what they DO. Don't document each function in its own subsection unless it is genuinely unique. Helper functions, similar CRUD functions, and lazy-init client factories should be summarized as one paragraph naming the group, not enumerated.
- State each pattern ONCE. If five functions all read SSM the same way, write "The handler reads SSM parameters via a cached client" once and list which functions use it. Don't repeat the pattern in each function's section.
- Skip cross-references unless they're load-bearing for understanding (e.g. an unobvious dependency on another Lambda). Don't list every shared utility import.
- If a function is trivial (one-line wrapper, getter, formatter), do not document it. Mention by name only.

Cover these themes, blended into prose -- not separate per-function rubric:

1. Module role + trigger model. What invokes the handler (SQS, EventBridge, API Gateway, DynamoDB Streams). Top-level dispatch logic.
2. The principal user-facing functions (typically 2-5 per handler). For each, briefly: what it does, what calls it, what it returns on success vs failure (outcome category, not exact codes), and the one or two pitfalls that matter.
3. Datastore / external-system interactions, by KIND not by exact identifier. Name the FIELDS schema, not the table-name strings. Mention "writes to a DynamoDB table keyed by a customer-scoped prefix" rather than listing exact key strings.
4. Notable design decisions or non-obvious behaviors that a debugger would benefit from knowing (e.g. "blacklisted users receive a 200 silently to avoid enumeration").

Skip:
- Exhaustive parameter tables. Name the inputs in prose.
- Function-by-function signature blocks. The symbol graph already carries signatures.
- Cross-reference link dumps. The reader has source access.
- Pitfall examples for trivial mistakes. Only flag pitfalls that take >5min to debug.

{files_section}
"""

# Bedrock pricing per 1M tokens (USD). Match tfvars defaults for prd:
#   bedrock_advanced_input_price  = 3.0   (Sonnet 4.6 input)
#   bedrock_advanced_output_price = 15.0  (Sonnet 4.6 output)
#   bedrock_basic_input_price     = 0.80  (Haiku 4.5 input)
#   bedrock_basic_output_price    = 4.00  (Haiku 4.5 output)
#   Opus 4.7: $15 input / $75 output (5x Sonnet, no local tfvar; hardcode
#   here since it's not a production tier).
PRICING = {
    "advanced": {"input": 3.00, "output": 15.00, "display": "sonnet"},
    "basic": {"input": 0.80, "output": 4.00, "display": "haiku"},
    "opus": {"input": 15.00, "output": 75.00, "display": "opus"},
}

# Opus isn't wrapped in a code-maps workload profile -- experiment-only,
# invoked via direct model_id. Uses whichever US-region cross-region
# profile is available.
OPUS_MODEL_ID = "us.anthropic.claude-opus-4-7"


def _enumerate_files(lambda_dir: Path) -> list[Path]:
    """Every .py file under the Lambda dir, minus tests + pycache + venv."""
    out = []
    for f in sorted(lambda_dir.rglob("*.py")):
        parts = f.relative_to(REPO_ROOT).parts
        if any(p in ("tests", "__pycache__", ".venv") for p in parts):
            continue
        out.append(f)
    return out


def _build_files_section(lambda_dir: Path) -> str:
    """Concat every Lambda file into the process-batch-summary files_section
    shape ("File: <path>\\n```\\n<content>\\n```\\n"). Truncate each file at
    10000 chars to match production behavior."""
    chunks = []
    for f in _enumerate_files(lambda_dir):
        rel = f.relative_to(REPO_ROOT)
        content = f.read_text(errors="replace")
        truncated = content[:10000]
        marker = "... (truncated)\n" if len(content) > 10000 else ""
        chunks.append(f"File: {rel}\n```\n{truncated}\n{marker}```\n")
    return "\n".join(chunks)


def _build_prompt(lambda_name: str, files_section: str) -> str:
    group_name = f"lambda/{lambda_name}"
    body = HANDLER_GROUP_TEMPLATE.format(
        group_name=group_name,
        repo=REPO_NAME,
        files_section=files_section,
    )
    return f"{body}\n\n{CODE_MAP_LITERAL_DISCIPLINE}"


_BEDROCK_RUNTIME = boto3.client("bedrock-runtime", region_name="us-west-2")


def _run_tier(prompt: str, tier: str) -> dict:
    """code-maps profile ARN via shared.model_client for advanced/basic;
    direct boto3.converse for opus.

    Opus 4.7 dropped the temperature knob (Bedrock returns
    ValidationException `temperature is deprecated for this model`).
    shared.model_client.invoke has a temperature=0.0 default that gets
    sent regardless, so bypass it for opus and call converse directly
    with no inferenceConfig.temperature.
    """
    started = time.time()
    messages = [{"role": "user", "content": [{"text": prompt}]}]
    if tier == "opus":
        resp = _BEDROCK_RUNTIME.converse(
            modelId=OPUS_MODEL_ID,
            messages=messages,
            inferenceConfig={"maxTokens": 4096},
        )
        out = resp["output"]["message"]["content"]
        text = "".join(c.get("text", "") for c in out)
        usage = resp["usage"]
        return {
            "text": text,
            "input_tokens": usage["inputTokens"],
            "output_tokens": usage["outputTokens"],
            "elapsed_s": time.time() - started,
        }
    r = invoke(
        workload="code-maps",
        tier=tier,
        messages=messages,
        max_tokens=4096,
        temperature=0.1,
    )
    return {
        "text": r.text,
        "input_tokens": r.usage.input_tokens,
        "output_tokens": r.usage.output_tokens,
        "elapsed_s": time.time() - started,
    }


def _cost_usd(tier: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING[tier]
    return (
        (input_tokens / 1_000_000) * p["input"]
        + (output_tokens / 1_000_000) * p["output"]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "lambdas",
        nargs="+",
        help="Lambda dir names under lambda/ (e.g. mcp-server process-batch-summary)",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "experiment"),
        help="Output dir (default: %(default)s). Gitignored.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)

    totals = {"advanced": 0.0, "basic": 0.0, "opus": 0.0}
    rows = []

    for lambda_name in args.lambdas:
        lambda_dir = REPO_ROOT / "lambda" / lambda_name
        if not lambda_dir.is_dir():
            print(f"SKIP {lambda_name}: no such dir under lambda/", file=sys.stderr)
            continue

        print(f"\n=== {lambda_name} ===")
        files = _enumerate_files(lambda_dir)
        files_section = _build_files_section(lambda_dir)
        prompt = _build_prompt(lambda_name, files_section)
        # Rough estimate: ~4 chars per token for English + Python source.
        est_input = len(prompt) // 4
        print(
            f"  files:{len(files)}  prompt_chars:{len(prompt)}  est_input_tok:{est_input}"
        )

        for tier in ("advanced", "basic", "opus"):
            display = PRICING[tier]["display"]
            print(f"  invoking {tier} ({display}) ...")
            try:
                result = _run_tier(prompt, tier)
            except Exception as e:
                print(f"    FAIL {tier}: {e}", file=sys.stderr)
                continue
            slug = f"{lambda_name}.{display}"
            (out_dir / f"{slug}.md").write_text(result["text"])
            c = _cost_usd(tier, result["input_tokens"], result["output_tokens"])
            totals[tier] += c
            rows.append(
                {
                    "lambda": lambda_name,
                    "tier": tier,
                    "display": display,
                    "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"],
                    "cost": c,
                    "elapsed_s": result["elapsed_s"],
                    "out_chars": len(result["text"]),
                }
            )
            print(
                f"    {display}: in={result['input_tokens']:,}  "
                f"out={result['output_tokens']:,}  "
                f"cost=${c:.4f}  elapsed={result['elapsed_s']:.1f}s  "
                f"out_chars={len(result['text']):,}  -> {out_dir}/{slug}.md"
            )

    print("\n=== SUMMARY ===")
    print(f"{'lambda':30s} {'tier':10s} {'in_tok':>10s} {'out_tok':>10s} {'cost_$':>10s} {'sec':>6s}")
    for r in rows:
        print(
            f"{r['lambda']:30s} {r['display']:10s} {r['input_tokens']:>10,d} "
            f"{r['output_tokens']:>10,d} {r['cost']:>10.4f} {r['elapsed_s']:>6.1f}"
        )
    if totals["advanced"] > 0:
        print(
            f"\nTOTAL sonnet=${totals['advanced']:.4f}  "
            f"haiku=${totals['basic']:.4f}  "
            f"opus=${totals['opus']:.4f}"
        )
        haiku_ratio = totals["basic"] / totals["advanced"]
        opus_ratio = totals["opus"] / totals["advanced"]
        print(
            f"vs sonnet: haiku={haiku_ratio*100:.1f}% "
            f"({100.0*(1.0-haiku_ratio):.1f}% cheaper), "
            f"opus={opus_ratio*100:.1f}% "
            f"({100.0*(opus_ratio-1.0):.1f}% more expensive)"
        )
    print("\nCompare qualitatively:")
    seen = set()
    for r in rows:
        base = r["lambda"]
        if base in seen:
            continue
        seen.add(base)
        print(
            f"  vimdiff {out_dir}/{base}.sonnet.md {out_dir}/{base}.haiku.md "
            f"{out_dir}/{base}.opus.md"
        )


if __name__ == "__main__":
    main()
