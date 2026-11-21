# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
ADR Compliance check handler.

Analyzes Lambda handlers and Terraform files against ADR standards using Claude.
Fetches PR file diffs from GitHub or Azure DevOps and uses Bedrock Claude to
analyze compliance.
"""

import json
import logging
from typing import Dict, List, Any, Literal, Optional

import boto3
import requests
from botocore.exceptions import ClientError

from .bedrock_utils import call_bedrock, UsageTracker, SONNET_MODEL_ID
from .ado_context import refresh_ado_token


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    so module load order (check_handlers before sys.path fix) is safe."""
    from shared.github_base import github_api_base
    return github_api_base()


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
lambda_client = boto3.client("lambda")
ssm_client = boto3.client("ssm")

CheckStatus = Literal["PASS", "WARN", "FAIL"]


def get_github_token(parameter_name: str) -> str:
    """Fetch GitHub token from SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )

        if not response.get("Parameter", {}).get("Value"):
            raise Exception(f"GitHub token not found in parameter: {parameter_name}")

        logger.info(f"Retrieved GitHub token from SSM: {parameter_name}")
        return response["Parameter"]["Value"]

    except ClientError as e:
        logger.error(f"Failed to retrieve GitHub token: {e}")
        raise


def fetch_pr_file_diff(repository: str, pr_number: int, file_path: str, token: str) -> str:
    """
    Fetch a single file's diff from GitHub PR.

    GitHub doesn't provide per-file diffs directly, so we fetch the full PR diff
    and extract the relevant file section.
    """
    # Fetch PR diff in unified diff format
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",  # Request diff format
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        full_diff = response.text

        # Extract the diff for the specific file
        # Diffs are separated by "diff --git" headers
        file_diff = extract_file_diff_from_full_diff(full_diff, file_path)

        return file_diff if file_diff else f"diff --git a/{file_path} b/{file_path}\n(No diff available)"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR diff: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def extract_file_diff_from_full_diff(full_diff: str, file_path: str) -> str:
    """Extract a specific file's diff section from the full PR diff."""
    lines = full_diff.split("\n")

    # Find the start of this file's diff
    file_diff_lines = []
    capturing = False

    for i, line in enumerate(lines):
        # Check if this is the start of our file's diff
        if line.startswith("diff --git") and file_path in line:
            capturing = True
            file_diff_lines.append(line)
        # Check if we've hit the next file's diff
        elif capturing and line.startswith("diff --git"):
            break
        # Capture lines for our file
        elif capturing:
            file_diff_lines.append(line)

    return "\n".join(file_diff_lines) if file_diff_lines else ""


def _added_line_numbers(file_diff: str) -> List[int]:
    """1-based RIGHT-side line numbers of added ("+") lines in a unified diff.

    Walks the hunk headers (``@@ -a,b +c,d @@``) to track the post-change line
    counter, collecting the line number of every added line in order. Used to
    (a) validate a model-supplied violation line against an actual changed line
    and (b) supply the first added line as an anchor fallback -- ADR-031 forbids
    anchoring a finding to a guessed line.
    """
    added: List[int] = []
    new_line = 0
    in_hunk = False

    for raw in file_diff.split("\n"):
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@  -- post-change side starts at c
            try:
                plus = raw.split("+", 1)[1].strip()
                new_line = int(plus.split(",", 1)[0].split()[0])
                in_hunk = True
            except (IndexError, ValueError):
                in_hunk = False
            continue
        if not in_hunk:
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            added.append(new_line)
            new_line += 1
        elif raw.startswith("-"):
            # Removed line -- no advance on the post-change side.
            continue
        elif raw.startswith(" "):
            # Context line -- advances the post-change side.
            new_line += 1
        # Anything else (blank split artifact, "\ No newline...") is ignored.

    return added


def _build_findings(
    file_path: str,
    file_diff: str,
    analysis: Dict[str, Any],
    severity: Literal["info", "warning", "error"],
) -> List[Dict[str, Any]]:
    """Turn a file's Claude ``violations`` into inline-comment Findings.

    One :class:`shared.pr_findings.Finding` per concrete violation, anchored to
    the model-supplied line when it matches an actual added ("+") line, else to
    the file's first added line; a violation with neither available is omitted
    (never fabricate a line). Returns ``model_dump()`` dicts.
    """
    from shared.pr_findings import Finding

    added = _added_line_numbers(file_diff)
    added_set = set(added)
    first_added = added[0] if added else None

    findings: List[Dict[str, Any]] = []
    for violation in analysis.get("violations", []):
        if not isinstance(violation, dict):
            continue

        message = violation.get("message") or analysis.get("explanation") or ""
        message = str(message).strip()
        if not message:
            continue

        line = violation.get("line")
        if isinstance(line, bool):
            line = None  # guard: bool is an int subclass
        elif isinstance(line, float):
            line = int(line)
        elif isinstance(line, str):
            line = int(line) if line.strip().isdigit() else None

        if isinstance(line, int) and line in added_set:
            anchor = line
        elif first_added is not None:
            anchor = first_added
        else:
            # No model line and no added line to fall back to -- omit.
            continue

        findings.append(
            Finding(
                path=file_path,
                line=anchor,
                side="RIGHT",
                severity=severity,
                message=message,
                enclosing_symbol=None,
            ).model_dump()
        )

    return findings


def _fetch_file_diff(
    *,
    provider: str,
    file_path: str,
    repository: str,
    pr_number: int,
    # GitHub context (unused for ADO)
    github_token: Optional[str] = None,
    # ADO context (unused for GitHub)
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    # GitLab context (unused for GitHub/ADO; project path == repository)
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> str:
    """Provider-dispatching per-file diff fetcher.

    On GitHub, uses the existing ``fetch_pr_file_diff`` which pulls the
    server-side unified diff via ``Accept: application/vnd.github.v3.diff``.

    On Azure DevOps and GitLab there is no equivalent server-side
    per-file diff endpoint, so we fetch base and head file bytes at the
    SHAs already computed by the caller (via
    ``shared.pr_metadata.fetch_pr_metadata``) and produce a unified diff
    client-side via ``shared.pr_diff.compute_file_diff``.
    """
    if provider == "github":
        return fetch_pr_file_diff(repository, pr_number, file_path, github_token)

    if provider in ("azure_devops", "gitlab"):
        # Late imports so unit tests can load this module without the
        # shared/ package being on sys.path at import time (the parent
        # handler.py sets that up).
        from shared.source_fetch import fetch_file_at_sha, SourceFetchError
        from shared.pr_diff import compute_file_diff

        if provider == "azure_devops":
            fetch_kwargs = dict(
                ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
                ado_access_token=ado_access_token,
            )
        else:
            fetch_kwargs = dict(
                gitlab_project_path=repository,
                gitlab_access_token=gitlab_access_token,
                gitlab_base_url=gitlab_base_url,
            )

        base_bytes: Optional[bytes]
        head_bytes: Optional[bytes]
        try:
            base_bytes = fetch_file_at_sha(
                provider, file_path, base_sha, **fetch_kwargs,
            )
        except SourceFetchError:
            base_bytes = None  # File added — did not exist in base
        try:
            head_bytes = fetch_file_at_sha(
                provider, file_path, head_sha, **fetch_kwargs,
            )
        except SourceFetchError:
            head_bytes = None  # File deleted in head

        return compute_file_diff(base_bytes, head_bytes, file_path)

    raise ValueError(f"Unknown provider: {provider}")


def query_knowledge_base(
    lambda_name: str, query: str, top_k: int = 3,
    workspace_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Query knowledge base via query-kb Lambda.

    ``workspace_ids`` scopes retrieval to those workspaces only
. Empty/None ==
    workspace-blind (pre-fix legacy behavior; callers should
    always pass a scope now).
    """
    payload = {
        "query": query,
        "topK": top_k,
    }
    if workspace_ids:
        payload["workspaceIds"] = workspace_ids

    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            Payload=json.dumps(payload)
        )

        response_payload = json.loads(response["Payload"].read())

        # Parse the body field (query-kb returns answer/sources inside body)
        body = json.loads(response_payload.get("body", "{}"))

        return {
            "answer": body.get("answer", ""),
            "sources": body.get("sources", [])
        }

    except ClientError as e:
        logger.error(f"Failed to query knowledge base: {e}")
        raise


def _extract_adr_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract the ADR-analysis JSON object from a model response.

    The model is asked for JSON only, but sometimes preambles with reasoning
    prose or a numbered list -- and that prose can itself contain braces (e.g. a
    quoted ``{"statusCode": 200, ...}`` code snippet), which defeats a naive
    first-'{'-to-last-'}' slice (that was the 2026-10-09 PR #92 failure: the
    slice spanned a prose snippet brace through the real JSON and would not
    parse). Scan each '{' as a candidate start, do a string-aware balanced-brace
    match to its close, and return the first object that parses AND carries a
    "compliant" key (our answer shape); a braceful prose snippet lacks that key
    and is skipped.
    """
    cleaned = text.replace("```json", "").replace("```", "")
    i, n = 0, len(cleaned)
    while i < n:
        if cleaned[i] != "{":
            i += 1
            continue
        depth, in_str, esc, close = 0, False, False, -1
        for j in range(i, n):
            c = cleaned[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close == -1:
            break
        try:
            obj = json.loads(cleaned[i:close + 1])
        except json.JSONDecodeError:
            obj = None
        # Only the real answer object carries "compliant"; braceful prose
        # snippets (e.g. a quoted {"statusCode": 200}) are skipped. If none is
        # found, return None so the caller takes its explicit parse-failure path
        # rather than treating a snippet as a verdict.
        if isinstance(obj, dict) and "compliant" in obj:
            return obj
        i = close + 1
    return None


def analyze_code_with_claude(
    file_path: str,
    file_diff: str,
    standards: str,
    file_type: Literal["lambda", "terraform"],
    usage_tracker: UsageTracker,
    workspace_id: str = ""
) -> Dict[str, Any]:
    """
    Ask Claude to analyze code against ADR standards.

    Returns:
        dict with keys: compliant (bool), explanation (str),
        suggestions (list), violations (list). Each ``violations`` entry is a
        dict ``{"line": <int|None>, "message": <str>}`` naming one concrete ADR
        violation and the 1-based RIGHT-side (post-change) line number of the
        offending added ("+") diff line, so the caller can post it as an inline
        PR comment. ``line`` is ``None`` when the model could not pin one.
    """
    file_type_label = "Lambda handler" if file_type == "lambda" else "Terraform file"

    focus_areas = (
        "Pydantic schemas, error handling, type safety, proper exports, logging"
        if file_type == "lambda"
        else "terraform-aws-modules usage, version pinning, KMS encryption, IAM policies, naming conventions"
    )

    prompt = f"""You are analyzing a {file_type_label} in a pull request to check compliance with our ADR standards.

FILE: {file_path}

ADR STANDARDS:
{standards}

FILE DIFF (changes being made):
```diff
{file_diff}
```

TASK:
Analyze ONLY the provided diff above. Determine if the code changes follow our ADR standards.

IMPORTANT RULES:
1. ONLY analyze what you can see in the diff
2. Do NOT make assumptions about code not shown in the diff
3. Do NOT hallucinate or invent code that isn't in the diff
4. Focus on what's being ADDED (+ lines) in the diff
5. Look for: {focus_areas}
6. For EACH distinct ADR violation, record the exact line number of the
   offending ADDED ("+") line. Use the 1-based line number on the NEW
   (post-change / RIGHT) side of the file, as implied by the diff hunk
   headers (@@ -old,n +new,n @@). If you genuinely cannot pin a single line
   for a violation, set its "line" to null -- do NOT guess a line.

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks). Begin your reply
with {{ and write nothing before or after the JSON -- no reasoning, no preamble,
no numbered list:
{{"compliant": true/false, "explanation": "brief explanation", "suggestions": ["suggestion 1", "suggestion 2"], "violations": [{{"line": 42, "message": "the one concrete ADR violation at this line"}}]}}

Each "violations" entry describes ONE concrete issue at ONE line. Do NOT put
aggregate counts (e.g. "3 violations") in any message -- enumerate each issue
as its own entry instead.

IMPORTANT: Only set compliant to false and provide suggestions/violations if there are ACTUAL ADR violations visible in the diff. Do NOT make suggestions for:
- Things that cannot be verified from the diff alone
- General best practices that are not ADR requirements
- Assumptions about code outside the diff

Examples:
- If code follows standards: {{"compliant": true, "explanation": "Handler uses Pydantic schemas and proper error handling", "suggestions": [], "violations": []}}
- If missing standards: {{"compliant": false, "explanation": "Handler is missing Pydantic schema validation", "suggestions": ["Add a Pydantic model for request validation", "Add try-except error handling"], "violations": [{{"line": 17, "message": "Request body parsed with json.loads instead of a Pydantic model"}}, {{"line": 23, "message": "Handler body has no try-except error handling"}}]}}
- If cannot determine from diff: {{"compliant": true, "explanation": "Changes maintain existing patterns, no violations detected in diff", "suggestions": [], "violations": []}}"""

    logger.info(f"Calling Claude to analyze {file_type} compliance for {file_path}...")

    try:
        response_text, usage = call_bedrock(
            prompt=prompt,
            # 5.x models are more verbose; 1000 tokens truncated the
            # explanation+suggestions+violations JSON mid-stream (stop_reason
            # MAX_TOKENS), which dropped real violations. Give ample headroom.
            model_id=SONNET_MODEL_ID,
            max_tokens=4000,
            temperature=0.1,
            workspace_id=workspace_id
        )
        usage_tracker.add(usage)

        logger.info(f"Claude response for {file_path}: {response_text}")

        # Parse JSON response -- tolerant of preamble prose and braces inside
        # that prose (see _extract_adr_json); returns the object with "compliant".
        parsed = _extract_adr_json(response_text)
        if parsed is not None:
            return {
                "compliant": parsed.get("compliant", False),
                "explanation": parsed.get("explanation", "No explanation provided"),
                "suggestions": parsed.get("suggestions", []) if isinstance(parsed.get("suggestions"), list) else [],
                "violations": parsed.get("violations", []) if isinstance(parsed.get("violations"), list) else [],
            }

        logger.error(
            f"Failed to extract ADR JSON from Claude response for {file_path}: "
            f"{response_text[:800]}"
        )
        # Fail CLOSED. An unparseable/truncated response must NOT be guessed as
        # compliant: the old substring check matched "true" inside a suggestion
        # (e.g. "attach_policy_statements = true") and passed files whose JSON
        # explicitly said "compliant": false. Surface a manual-review WARN.
        return {
            "compliant": False,
            "explanation": "Could not parse automated ADR analysis (response may be truncated); manual review required.",
            "suggestions": [],
            "violations": [],
        }

    except Exception as e:
        # Governance check: an analysis error (MAX_TOKENS/no-text, transient
        # model failure) must never become a silent pass. Fail CLOSED to a
        # manual-review WARN instead of propagating and letting the caller skip
        # the file (which read as compliant).
        logger.error(f"Error analyzing {file_path} for ADR compliance: {e}")
        return {
            "compliant": False,
            "explanation": f"Automated ADR analysis could not complete ({e}); manual review required.",
            "suggestions": [],
            "violations": [],
        }


def analyze_lambda_handlers(
    repository: str,
    pr_number: int,
    files: List[str],
    query_kb_lambda_name: str,
    github_token: Optional[str],
    usage_tracker: UsageTracker,
    *,
    workspace_ids: Optional[List[str]] = None,
    workspace_id: str = "",
    provider: str = "github",
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze Lambda handlers against ADR standards using Claude."""
    # Filter for Lambda handler files (exclude tests, shared libs)
    handler_files = [
        f for f in files
        if f.startswith("lambda/") and f.endswith("/handler.py")
        and "/tests/" not in f
    ]

    if not handler_files:
        return {
            "status": "PASS",
            "message": "No Lambda handler files changed",
            "details": [],
            "findings": []
        }

    # Query KB for Lambda standards, scoped to the workspaces the
    # ADR check should walk (repo's workspace + all is_global).
    kb_result = query_knowledge_base(
        query_kb_lambda_name,
        "What are our Lambda handler standards? Include requirements for Pydantic validation, error handling, documentation, and testing.",
        top_k=5,
        workspace_ids=workspace_ids,
    )

    logger.info(f"Lambda KB Result: {kb_result['answer']}")

    # Analyze each handler file with Claude
    details = []
    findings: List[Dict[str, Any]] = []
    has_non_compliant = False

    for file in handler_files:
        try:
            # Fetch file diff from the source provider
            file_diff = _fetch_file_diff(
                provider=provider,
                file_path=file,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_repo=ado_repo,
                ado_access_token=ado_access_token,
                gitlab_access_token=gitlab_access_token,
                gitlab_base_url=gitlab_base_url,
                base_sha=base_sha,
                head_sha=head_sha,
            )

            # Analyze with Claude
            analysis = analyze_code_with_claude(
                file, file_diff, kb_result["answer"], "lambda", usage_tracker,
                workspace_id=workspace_id,
            )

            if not analysis["compliant"]:
                has_non_compliant = True
                details.append(f"**{file}**: {analysis['explanation']}")
                if analysis["suggestions"]:
                    details.append("  Suggestions:")
                    for suggestion in analysis["suggestions"]:
                        details.append(f"  - {suggestion}")
                # Line-anchored inline findings. Non-compliant file == WARN
                # verdict for this advisory check, so severity is "warning".
                findings.extend(
                    _build_findings(file, file_diff, analysis, "warning")
                )
            # Don't show details for compliant files - keep output clean

        except Exception as e:
            logger.error(f"Error analyzing {file}: {e}")
            details.append(f"**{file}**: Analysis failed - {str(e)}")

    # Determine overall status
    if has_non_compliant:
        status = "WARN"  # Use WARN for advisory-only
        message = f"Found compliance issues in Lambda handlers"
    else:
        status = "PASS"
        message = f"All Lambda handlers follow ADR standards"

    return {
        "status": status,
        "message": message,
        "details": details,
        "findings": findings
    }


def analyze_terraform_files(
    repository: str,
    pr_number: int,
    files: List[str],
    query_kb_lambda_name: str,
    github_token: Optional[str],
    usage_tracker: UsageTracker,
    *,
    workspace_ids: Optional[List[str]] = None,
    workspace_id: str = "",
    provider: str = "github",
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    gitlab_base_url: Optional[str] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze Terraform files against module standards using Claude."""
    terraform_files = [f for f in files if f.startswith("terraform/") and f.endswith(".tf")]

    if not terraform_files:
        return {
            "status": "PASS",
            "message": "No Terraform files changed",
            "details": [],
            "findings": []
        }

    # Query KB for Terraform standards, scoped to the workspaces the
    # ADR check should walk (repo's workspace + all is_global).
    kb_result = query_knowledge_base(
        query_kb_lambda_name,
        "What are our Terraform module standards? Include requirements for terraform-aws-modules, version pinning, and naming conventions.",
        top_k=5,
        workspace_ids=workspace_ids,
    )

    logger.info(f"Terraform KB Result: {kb_result['answer']}")

    # Analyze each Terraform file with Claude
    details = []
    findings: List[Dict[str, Any]] = []
    has_non_compliant = False

    for file in terraform_files:
        try:
            # Fetch file diff from the source provider
            file_diff = _fetch_file_diff(
                provider=provider,
                file_path=file,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_repo=ado_repo,
                ado_access_token=ado_access_token,
                gitlab_access_token=gitlab_access_token,
                gitlab_base_url=gitlab_base_url,
                base_sha=base_sha,
                head_sha=head_sha,
            )

            # Analyze with Claude
            analysis = analyze_code_with_claude(
                file, file_diff, kb_result["answer"], "terraform", usage_tracker,
                workspace_id=workspace_id,
            )

            if not analysis["compliant"]:
                has_non_compliant = True
                details.append(f"**{file}**: {analysis['explanation']}")
                if analysis["suggestions"]:
                    details.append("  Suggestions:")
                    for suggestion in analysis["suggestions"]:
                        details.append(f"  - {suggestion}")
                # Line-anchored inline findings. Non-compliant file == WARN
                # verdict for this advisory check, so severity is "warning".
                findings.extend(
                    _build_findings(file, file_diff, analysis, "warning")
                )
            # Don't show details for compliant files - keep output clean

        except Exception as e:
            logger.error(f"Error analyzing {file}: {e}")
            details.append(f"**{file}**: Analysis failed - {str(e)}")

    # Determine overall status
    if has_non_compliant:
        status = "WARN"
        message = f"Found compliance issues in Terraform files"
    else:
        status = "PASS"
        message = f"All Terraform files follow module standards"

    return {
        "status": status,
        "message": message,
        "details": details,
        "findings": findings
    }


def check_adr_compliance(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    query_kb_lambda_name: str,
    github_token_param: str,
    *,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
) -> Dict[str, Any]:
    """
    ADR Compliance check handler.

    Checks:
    - Lambda handlers follow ADR standards (Pydantic validation, error handling, documentation)
    - Terraform files follow module standards (terraform-aws-modules, version pinning)
    - Uses Claude to analyze actual code diffs against KB standards

    Provider-aware: on ``provider="github"`` fetches per-file diffs
    via GitHub's server-side unified-diff endpoint. On
    ``provider="azure_devops"`` refreshes the customer's ADO OAuth
    token, fetches PR head/base commit SHAs via
    ``shared.pr_metadata.fetch_pr_metadata``, then computes each
    file's diff client-side from base+head file bytes via
    ``shared.pr_diff.compute_file_diff``.
    """
    logger.info(
        f"Running ADR compliance check for PR #{pr_number} "
        f"(provider={provider})"
    )

    # Create usage tracker
    usage_tracker = UsageTracker()

    try:
        # Provider-specific auth + PR context prep.
        github_token: Optional[str] = None
        ado_access_token: Optional[str] = None
        gitlab_access_token: Optional[str] = None
        gitlab_base_url: Optional[str] = None
        base_sha: Optional[str] = None
        head_sha: Optional[str] = None

        if provider == "github":
            github_token = get_github_token(github_token_param)
        elif provider == "azure_devops":
            # Late import so unit tests can load this module without
            # the shared/ package on sys.path at import time.
            from shared.pr_metadata import fetch_pr_metadata

            ado_access_token = refresh_ado_token(connection_id)
            pr = fetch_pr_metadata(
                "azure_devops", pr_number,
                ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
                ado_access_token=ado_access_token,
            )
            base_sha = pr.base_sha
            head_sha = pr.head_sha
        elif provider == "gitlab":
            from shared.pr_metadata import fetch_pr_metadata
            from .gitlab_context import get_gitlab_token, get_gitlab_base_url

            # ``repository`` IS the full project path (variable-depth slug).
            gitlab_access_token = get_gitlab_token(connection_id)
            gitlab_base_url = get_gitlab_base_url(connection_id)
            pr = fetch_pr_metadata(
                "gitlab", pr_number,
                gitlab_project_path=repository,
                gitlab_access_token=gitlab_access_token,
                gitlab_base_url=gitlab_base_url,
            )
            base_sha = pr.base_sha
            head_sha = pr.head_sha
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Resolve per-check retrieval scope. ADR_COMPLIANCE is a
        # "standards" check: walks the repo's workspace(s) + all
        # is_global workspaces so enterprise-tier ADRs apply.
        # See lambda/shared/pr_check_scope.py. Fail-soft on any
        # error (DDB access, missing table, etc.) -- retrieval falls
        # back to workspace-blind, matching pre-fix behavior.
        workspace_ids: Optional[List[str]] = None
        try:
            from shared.pr_check_scope import resolve_pr_check_workspace_scope
            workspace_ids = resolve_pr_check_workspace_scope(
                check_type=check_type, repo_name=repository,
                provider=provider,
            )
        except Exception as scope_exc:
            logger.warning(
                f"Could not resolve workspace scope for {check_type}: "
                f"{scope_exc}. Falling back to workspace-blind retrieval."
            )

        # Attribute PR-analyzer Bedrock spend to the PR repo's OWNING workspace
        # (the first repo-row owner for this provider), NOT the retrieval scope above (which
        # spans the repo's workspaces + all is_global). Fail-soft: "" ->
        # WS#SYSTEM (unattributed) rather than breaking the check.
        owning_ws = ""
        try:
            from shared.pr_check_scope import find_workspaces_for_repo
            owning_ws = (sorted(find_workspaces_for_repo(repository, provider=provider)) or [""])[0]
        except Exception as ws_exc:
            logger.warning(
                f"Could not resolve owning workspace for spend attribution: "
                f"{ws_exc}"
            )

        # Analyze Lambda handlers and Terraform files with Claude
        lambda_result = analyze_lambda_handlers(
            repository, pr_number, changed_files, query_kb_lambda_name,
            github_token, usage_tracker,
            workspace_ids=workspace_ids,
            workspace_id=owning_ws,
            provider=provider,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
            gitlab_access_token=gitlab_access_token,
            gitlab_base_url=gitlab_base_url,
            base_sha=base_sha, head_sha=head_sha,
        )

        terraform_result = analyze_terraform_files(
            repository, pr_number, changed_files, query_kb_lambda_name,
            github_token, usage_tracker,
            workspace_ids=workspace_ids,
            workspace_id=owning_ws,
            provider=provider,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
            gitlab_access_token=gitlab_access_token,
            gitlab_base_url=gitlab_base_url,
            base_sha=base_sha, head_sha=head_sha,
        )

        # Combine results
        all_details = lambda_result["details"] + terraform_result["details"]
        all_findings = (
            lambda_result.get("findings", []) + terraform_result.get("findings", [])
        )

        # Determine overall status (FAIL > WARN > PASS)
        if lambda_result["status"] == "FAIL" or terraform_result["status"] == "FAIL":
            overall_status = "FAIL"
        elif lambda_result["status"] == "WARN" or terraform_result["status"] == "WARN":
            overall_status = "WARN"
        else:
            overall_status = "PASS"

        message = (
            "All files follow ADR standards"
            if overall_status == "PASS"
            else f"{lambda_result['message']}. {terraform_result['message']}"
        )

        return {
            "checkType": check_type,
            "status": overall_status,
            "message": message,
            "details": all_details,
            "findings": all_findings,
            "usage": usage_tracker
        }

    except Exception as e:
        logger.error(f"Error in ADR compliance check: {e}", exc_info=True)
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "ADR compliance check failed",
            "details": [
                "Could not complete automated analysis",
                "Please manually review code for ADR compliance",
                f"Error: {str(e)}"
            ],
            "findings": [],
            "usage": usage_tracker
        }
