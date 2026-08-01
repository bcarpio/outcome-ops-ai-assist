#!/usr/bin/env python3
"""HA primary/secondary parity checker (ADR-022).

Every primary-region terraform module with a ``*_secondary`` mirror must
keep three surfaces in sync, or the divergence stays invisible until
failover -- the worst time to find it:

1. environment_variables KEY sets (values legitimately differ: regional
   table/queue/KMS ARNs -- but a key present on one side only means one
   region silently runs different behavior).
2. policy_statements statement NAMES (a grant added to the primary but
   not the mirror = broken IAM post-failover).
3. merge()'d locals, normalized (``local.ha_writer_env`` on the primary
   pairs with ``local.ha_writer_env_secondary`` on the mirror).

Found six drifted modules on first run (2026-07-20) -- accumulated by
four independent work streams each updating only the primary. CI runs
this via ``make check-ha-parity`` so the class can't merge again.

Pure stdlib + regex on purpose: no HCL parser dependency, so it runs in
any CI python without installs. The regex contract it relies on is the
house terraform style (module blocks at column 0, two-space indent,
``environment_variables = merge(`` / ``= {`` and ``policy_statements =
merge(`` / ``= {``); if a module is restructured beyond that, this
script fails loudly with a parse error rather than silently passing.

Exit codes: 0 clean, 1 drift found, 2 parse/structure error.
"""

from __future__ import annotations

import glob
import os
import re
import sys

TF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "terraform")

# Keys that are legitimately present on ONE side only. Every entry needs
# a reason -- an unexplained allowlist line is just drift with extra steps.
ALLOWED_SECONDARY_ONLY_ENV = {
    # Injected so the UI/services know which region served a request;
    # the primary derives it from the provider region instead.
    "THIS_REGION",
}
ALLOWED_PRIMARY_ONLY_ENV: dict[str, set] = {
    # module_name -> {keys}; document the ADR that records the divergence.
}
ALLOWED_IAM_DIVERGENCE: dict[str, set] = {
    # module_name -> {statement names}; document why.
    #
    # The primary region grants symbol-graph access via STANDALONE
    # aws_iam_policy + role_policy_attachment resources in
    # iam_symbol_graph.tf (shared across chat / analyze-pr /
    # process-batch-summary / delete-worker). The secondaries inline the
    # equivalent grants as policy_statements (see docs/HA.md:
    # "symbol-graph reader IAM is inlined per-region"). Equivalent
    # permissions, different terraform mechanism -- this checker only
    # sees the inline side.
    "chat_streaming_lambda": {"symbol_graph_read"},
    "process_batch_summary_lambda": {"symbol_graph_write"},
}


def _find_module_block(name: str, files: list[str]) -> str | None:
    """Return the body text of ``module "<name>"`` up to its tags block."""
    pat = re.compile(rf'^module "{re.escape(name)}" ', re.M)
    for path in files:
        text = open(path).read()
        m = pat.search(text)
        if not m:
            continue
        tail = text[m.end():]
        # Body runs to the module's tags block OR its closing brace (a "}" at
        # column 0), whichever comes first. Not every module sets tags -- the
        # mcp-server catalog servers (snyk/sonarqube) don't -- and keying only
        # on a tags block made those return None and report a false
        # "primary module block not found".
        end = re.search(r"\n  tags = \{|\ntags = \{|\n\}", tail)
        if not end:
            return None
        return tail[: end.start()]
    return None


def _extract(body: str, attr: str) -> str | None:
    m = re.search(rf"{attr} = merge\((.*?)\n  \)", body, re.S)
    if m:
        return m.group(1)
    m = re.search(rf"{attr} = \{{(.*?)\n  \}}", body, re.S)
    if m:
        return m.group(1)
    return None


def _env_keys(body: str) -> set | None:
    seg = _extract(body, "environment_variables")
    if seg is None:
        return None
    return set(re.findall(r"^\s+([A-Z][A-Z_0-9]*)\s+=", seg, re.M))


def _iam_sids(body: str) -> set | None:
    seg = _extract(body, "policy_statements")
    if seg is None:
        return None
    return set(re.findall(r"^\s+(\w+) = \{", seg, re.M))


def _merge_locals(body: str, attr: str) -> set:
    seg = _extract(body, attr) or ""
    locs = set(re.findall(r"local\.(\w+)", seg))
    # Normalize the secondary suffix so ha_writer_env pairs with
    # ha_writer_env_secondary.
    return {re.sub(r"_secondary$", "", x) for x in locs}


def main() -> int:
    primary_files = sorted(
        f for f in glob.glob(os.path.join(TF_DIR, "*.tf")) if "secondary" not in os.path.basename(f)
    )
    secondary_files = sorted(glob.glob(os.path.join(TF_DIR, "*-secondary.tf")))
    if not secondary_files:
        print("check-ha-parity: no *-secondary.tf files found", file=sys.stderr)
        return 2

    pairs = []
    for f in secondary_files:
        for name in re.findall(r'^module "(\w+)_secondary"', open(f).read(), re.M):
            pairs.append((name, f))

    if not pairs:
        print("check-ha-parity: no *_secondary modules found", file=sys.stderr)
        return 2

    drift = 0
    errors = 0
    checked = 0
    for name, sec_file in sorted(pairs):
        prim = _find_module_block(name, primary_files)
        sec = _find_module_block(f"{name}_secondary", [sec_file])
        if prim is None:
            print(f"ERROR {name}: primary module block not found (renamed? restructured?)")
            errors += 1
            continue
        if sec is None:
            print(f"ERROR {name}_secondary: block found but tags terminator missing")
            errors += 1
            continue
        checked += 1

        problems = []

        pk, sk = _env_keys(prim), _env_keys(sec)
        if (pk is None) != (sk is None):
            problems.append("environment_variables present on only one side")
        elif pk is not None:
            p_only = pk - sk - ALLOWED_PRIMARY_ONLY_ENV.get(name, set())
            s_only = sk - pk - ALLOWED_SECONDARY_ONLY_ENV
            if p_only:
                problems.append(f"env keys missing from secondary: {sorted(p_only)}")
            if s_only:
                problems.append(f"env keys only on secondary: {sorted(s_only)}")

        pi, si = _iam_sids(prim), _iam_sids(sec)
        if (pi is None) != (si is None):
            problems.append("policy_statements present on only one side")
        elif pi is not None:
            diff = (pi ^ si) - ALLOWED_IAM_DIVERGENCE.get(name, set())
            if diff:
                problems.append(
                    f"IAM statements differ: primary-only={sorted(pi - si)} "
                    f"secondary-only={sorted(si - pi)}"
                )

        pl = _merge_locals(prim, "environment_variables")
        sl = _merge_locals(sec, "environment_variables")
        if pl != sl:
            problems.append(
                f"env merge() locals differ (normalized): primary-only={sorted(pl - sl)} "
                f"secondary-only={sorted(sl - pl)}"
            )

        if problems:
            drift += 1
            print(f"DRIFT {name}:")
            for p in problems:
                print(f"    - {p}")

    print(
        f"check-ha-parity: {checked} mirrored module pairs checked, "
        f"{drift} drifted, {errors} parse errors"
    )
    if errors:
        return 2
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
