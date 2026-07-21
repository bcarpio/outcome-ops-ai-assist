"""
License Compliance check handler.

Scans source code files for:
- Prohibited license text (GPL, AGPL, SSPL)
- Allowed licenses (MIT, Apache, BSD)
- Copyright notices on new files
- Compliance with ADR-008 license policy
- AI code similarity detection (Phase 2)
"""

import hashlib
import json
import logging
import re
from typing import Dict, List, Any, Literal, Tuple, Optional

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

# License detection patterns based on ADR-008
PROHIBITED_PATTERNS = [
    (r'GNU General Public License', 'GPL'),
    (r'\bGPL-?[23]\.0\b', 'GPL'),
    (r'GNU Affero General Public License', 'AGPL'),
    (r'\bAGPL\b', 'AGPL'),
    (r'Server Side Public License', 'SSPL'),
    (r'\bSSPL\b', 'SSPL'),
]

ALLOWED_PATTERNS = [
    (r'MIT License', 'MIT'),
    (r'Apache License,?\s+Version 2\.0', 'Apache-2.0'),
    (r'BSD [23]-Clause License', 'BSD'),
    (r'ISC License', 'ISC'),
]

REVIEW_PATTERNS = [
    (r'GNU Lesser General Public License', 'LGPL'),
    (r'\bLGPL\b', 'LGPL'),
    (r'Mozilla Public License', 'MPL'),
    (r'\bMPL\b', 'MPL'),
]

# Source code file extensions to scan
SOURCE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx',
    '.go', '.java', '.rb', '.php', '.rs',
    '.c', '.cpp', '.h', '.hpp', '.scala',
    '.kt', '.swift', '.cs', '.sh', '.bash'
}

# Copyright header patterns
COPYRIGHT_PATTERN = r'Copyright\s+\d{4}(-\d{4})?\s+.+'
SPDX_PATTERN = r'SPDX-License-Identifier:\s+[A-Za-z0-9\.\-]+'

# Phase 2.1: Known GPL algorithm patterns (fast pattern matching)
GPL_ALGORITHM_PATTERNS = [
    # Common GPL sorting/data structure implementations
    (r'def\s+quicksort.*pivot.*partition', 'GPL quicksort implementation'),
    (r'class\s+BTree.*insert.*split', 'GPL B-tree algorithm'),
    (r'def\s+dijkstra.*priority.*queue', 'GPL graph algorithm'),

    # Common GPL cryptographic implementations
    (r'def\s+aes_encrypt.*rijndael', 'GPL AES implementation'),
    (r'class\s+RSA.*modular.*exponentiation', 'GPL RSA implementation'),

    # Common GPL web framework patterns
    (r'class\s+Router.*dispatch.*middleware', 'GPL router pattern'),
    (r'def\s+render_template.*jinja.*context', 'GPL template engine'),

    # JWT/Auth patterns commonly found in GPL projects
    (r'def\s+validate_jwt.*decode.*verify.*signature', 'GPL JWT validator'),
    (r'class\s+TokenManager.*refresh.*blacklist', 'GPL token manager'),
]

# Phase 2.1: Known GPL code hashes (exact matches)
# These will be populated over time via Phase 2.3 human review feedback
KNOWN_GPL_HASHES = {
    # Format: 'sha256_hash': {'project': 'name', 'license': 'GPL-3.0', 'description': 'what it does'}
    # Examples (these are placeholder hashes):
    # 'a1b2c3...': {'project': 'jwt-auth-gpl', 'license': 'GPL-3.0', 'description': 'JWT token validation'},
    # 'd4e5f6...': {'project': 'redis-cache-gpl', 'license': 'GPL-2.0', 'description': 'Redis cache implementation'},
}


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
    """
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        full_diff = response.text
        file_diff = extract_file_diff_from_full_diff(full_diff, file_path)

        return file_diff if file_diff else f"diff --git a/{file_path} b/{file_path}\n(No diff available)"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR diff: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def extract_file_diff_from_full_diff(full_diff: str, file_path: str) -> str:
    """Extract a specific file's diff section from the full PR diff."""
    lines = full_diff.split("\n")

    file_diff_lines = []
    capturing = False

    for line in lines:
        if line.startswith("diff --git") and file_path in line:
            capturing = True
            file_diff_lines.append(line)
        elif capturing and line.startswith("diff --git"):
            break
        elif capturing:
            file_diff_lines.append(line)

    return "\n".join(file_diff_lines) if file_diff_lines else ""


def normalize_code(code: str) -> str:
    """
    Normalize code for hashing (remove whitespace, comments).

    This allows detecting code similarity even when formatting differs.
    """
    # Remove multi-line comments first (before splitting by lines)
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)

    lines = []
    for line in code.split("\n"):
        # Remove single-line comments
        line = re.sub(r'#.*$', '', line)  # Python comments
        line = re.sub(r'//.*$', '', line)  # JS/Java/C++ comments

        # Strip whitespace
        line = line.strip()

        # Skip empty lines
        if line:
            lines.append(line)

    return "\n".join(lines)


def quick_pattern_check(code: str) -> Optional[Dict[str, Any]]:
    """
    Phase 2.1: Fast pattern matching for known GPL code patterns.

    Returns match details if suspicious pattern found, None otherwise.
    This prevents expensive Claude API calls on clean code.
    """
    # Check GPL algorithm patterns
    for pattern, description in GPL_ALGORITHM_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE | re.DOTALL):
            logger.info(f"Pattern match found: {description}")
            return {
                "match_type": "pattern",
                "description": description,
                "pattern": pattern
            }

    # Check code hashes for exact matches
    normalized = normalize_code(code)
    code_hash = hashlib.sha256(normalized.encode()).hexdigest()

    if code_hash in KNOWN_GPL_HASHES:
        match_info = KNOWN_GPL_HASHES[code_hash]
        logger.info(f"Hash match found: {match_info['project']} ({match_info['license']})")
        return {
            "match_type": "hash",
            "project": match_info['project'],
            "license": match_info['license'],
            "description": match_info['description'],
            "hash": code_hash
        }

    # No suspicious patterns found - skip expensive Claude analysis
    return None


def analyze_code_similarity_with_claude(
    code: str,
    file_path: str,
    pattern_match: Dict[str, Any],
    usage_tracker: UsageTracker
) -> Dict[str, Any]:
    """
    Phase 2.2: Claude semantic analysis for code similarity.

    Only called when Phase 2.1 detected a suspicious pattern.
    Uses Claude to determine if code resembles copyrighted OSS implementations.

    Returns:
        dict with keys: similar_projects (list), confidence, explanation
    """
    prompt = f"""Analyze this code for similarity to copyrighted open source projects.

FILE: {file_path}

INITIAL PATTERN MATCH: {pattern_match.get('description', 'Unknown pattern')}

CODE:
```
{code}
```

TASK:
1. What problem is this code solving?
2. Name 3 well-known open source projects that solve this problem similarly
3. For EACH project, provide:
   - Project name
   - License type (GPL, AGPL, MIT, Apache, etc.)
   - Similarity percentage (0-100)
   - Confidence level (HIGH/MEDIUM/LOW)
   - Specific patterns that match (algorithm structure, naming, edge cases)

CONFIDENCE LEVELS:
- HIGH (>75%): Algorithm + implementation structure + variable naming all match
- MEDIUM (40-75%): Algorithm structure or implementation pattern matches
- LOW (<40%): Only common patterns, no concern

RESPONSE FORMAT (JSON only, no markdown):
{{
  "problem_description": "brief description of what this code does",
  "similar_projects": [
    {{
      "name": "project-name",
      "license": "GPL-3.0",
      "similarity_percentage": 85,
      "confidence": "HIGH",
      "matching_patterns": ["algorithm structure", "variable naming", "edge case handling"]
    }},
    {{
      "name": "another-project",
      "license": "Apache-2.0",
      "similarity_percentage": 45,
      "confidence": "MEDIUM",
      "matching_patterns": ["similar algorithm approach"]
    }}
  ]
}}

IMPORTANT: Only flag GPL/AGPL/SSPL projects with HIGH or MEDIUM confidence."""

    logger.info(f"Calling Claude for semantic analysis of {file_path}...")

    try:
        output, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=2000,
            temperature=0.1
        )
        usage_tracker.add(usage)

        # Parse JSON response (handle markdown code blocks)
        output = output.strip()
        if output.startswith("```"):
            output = output.split("```")[1]
            if output.startswith("json"):
                output = output[4:]
        output = output.strip()

        result = json.loads(output)

        return {
            "problem_description": result.get("problem_description", ""),
            "similar_projects": result.get("similar_projects", []),
            "analysis_successful": True
        }

    except Exception as e:
        logger.error(f"Claude semantic analysis failed: {e}")
        # Fallback: treat pattern match as MEDIUM confidence
        return {
            "problem_description": pattern_match.get("description", "Unknown"),
            "similar_projects": [
                {
                    "name": "Unknown GPL project",
                    "license": "GPL-3.0",
                    "similarity_percentage": 50,
                    "confidence": "MEDIUM",
                    "matching_patterns": [pattern_match.get("description", "Pattern match")]
                }
            ],
            "analysis_successful": False
        }


def fetch_pr_commits(repository: str, pr_number: int, token: str) -> List[Dict[str, Any]]:
    """
    Fetch all commits in a PR from GitHub API.

    Returns list of commits with message, author, sha.
    """
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}/commits"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        commits = response.json()
        return [
            {
                "sha": commit["sha"],
                "message": commit["commit"]["message"],
                "author": commit["commit"]["author"]["name"]
            }
            for commit in commits
        ]

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR commits: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def is_ai_generated_commit(commit: Dict[str, Any]) -> bool:
    """
    Check if commit was generated by AI.

    Detects:
    - "Generated with Claude Code" in commit message
    - "Co-Authored-By: Claude <noreply@anthropic.com>" in commit message
    """
    message = commit.get("message", "")

    # Check for Claude Code marker
    if "Generated with Claude Code" in message or "Claude Code" in message:
        return True

    # Check for Claude co-author
    if "Co-Authored-By: Claude" in message:
        return True

    return False


def _fetch_license_file_diff(
    *,
    provider: str,
    file_path: str,
    repository: str,
    pr_number: int,
    github_token: Optional[str] = None,
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
    base_sha: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> str:
    """Provider-dispatching per-file diff for license/copyright scanning.

    On GitHub uses the server-side unified-diff endpoint. On ADO and
    GitLab fetches base+head bytes at PR SHAs and computes diff
    client-side. GitLab's project path IS ``repository``.
    """
    if provider == "github":
        return fetch_pr_file_diff(repository, pr_number, file_path, github_token)

    if provider in ("azure_devops", "gitlab"):
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
            )

        base_bytes: Optional[bytes]
        head_bytes: Optional[bytes]
        try:
            base_bytes = fetch_file_at_sha(
                provider, file_path, base_sha, **fetch_kwargs,
            )
        except SourceFetchError:
            base_bytes = None
        try:
            head_bytes = fetch_file_at_sha(
                provider, file_path, head_sha, **fetch_kwargs,
            )
        except SourceFetchError:
            head_bytes = None
        return compute_file_diff(base_bytes, head_bytes, file_path)

    raise ValueError(f"Unknown provider: {provider}")


def get_ai_generated_files(
    repository: str,
    pr_number: int,
    changed_files: List[str],
    token: Optional[str],
    *,
    provider: str = "github",
    ado_org: Optional[str] = None,
    ado_project: Optional[str] = None,
    ado_repo: Optional[str] = None,
    ado_access_token: Optional[str] = None,
    gitlab_access_token: Optional[str] = None,
) -> List[str]:
    """
    Filter changed files to only those from AI-generated commits.

    Returns subset of changed_files that were added/modified in AI commits.

    Provider-aware: dispatches through ``shared.pr_commits.fetch_pr_commits``
    which handles GitHub's full-message commits endpoint and ADO's
    truncated-comment endpoint (auto-refetches truncated entries so
    Co-Authored-By trailers are visible).
    """
    try:
        if provider in ("azure_devops", "gitlab"):
            from shared.pr_commits import fetch_pr_commits as _fetch_commits_shared
            if provider == "azure_devops":
                commits_kwargs = dict(
                    ado_org=ado_org, ado_project=ado_project,
                    ado_repo=ado_repo, ado_access_token=ado_access_token,
                )
            else:
                commits_kwargs = dict(
                    gitlab_project_path=repository,
                    gitlab_access_token=gitlab_access_token,
                )
            shared_commits = _fetch_commits_shared(
                provider, pr_number, **commits_kwargs,
            )
            # Adapt the shared NamedTuple shape to the dict shape
            # is_ai_generated_commit() expects.
            commits = [
                {"sha": c.sha, "message": c.message, "author": c.author}
                for c in shared_commits
            ]
        else:
            commits = fetch_pr_commits(repository, pr_number, token)

        ai_commits = [c for c in commits if is_ai_generated_commit(c)]

        if not ai_commits:
            logger.info("No AI-generated commits found in PR")
            return []

        logger.info(f"Found {len(ai_commits)} AI-generated commits out of {len(commits)} total")

        # For now, return all changed files if ANY commit is AI-generated
        # TODO: In future, fetch files per commit to be more precise
        return changed_files

    except Exception as e:
        logger.warning(f"Could not determine AI-generated commits: {e}")
        # Fallback: analyze all files if we can't determine commit source
        return []


def query_knowledge_base(
    lambda_name: str, query: str, top_k: int = 3,
    workspace_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Query knowledge base via query-kb Lambda.

    ``workspace_ids`` scopes retrieval to those workspaces (chunk 04
    of the workspace-scope plan).
    """
    payload: Dict[str, Any] = {
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
        body = json.loads(response_payload.get("body", "{}"))

        return {
            "answer": body.get("answer", ""),
            "sources": body.get("sources", [])
        }

    except ClientError as e:
        logger.error(f"Failed to query knowledge base: {e}")
        raise


def detect_license_patterns(text: str) -> Dict[str, List[str]]:
    """
    Scan text for license patterns.

    Returns:
        Dict with keys: prohibited, allowed, review_required
    """
    findings = {
        "prohibited": [],
        "allowed": [],
        "review_required": []
    }

    # Check prohibited licenses
    for pattern, license_name in PROHIBITED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            if license_name not in findings["prohibited"]:
                findings["prohibited"].append(license_name)

    # Check allowed licenses
    for pattern, license_name in ALLOWED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            if license_name not in findings["allowed"]:
                findings["allowed"].append(license_name)

    # Check review-required licenses
    for pattern, license_name in REVIEW_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            if license_name not in findings["review_required"]:
                findings["review_required"].append(license_name)

    return findings


def check_copyright_header(file_diff: str) -> bool:
    """
    Check if new file has copyright header.
    Only checks added lines (+ lines) in diff.
    """
    added_lines = [
        line[1:] for line in file_diff.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    ]

    content = "\n".join(added_lines)

    has_copyright = bool(re.search(COPYRIGHT_PATTERN, content, re.IGNORECASE))
    has_spdx = bool(re.search(SPDX_PATTERN, content, re.IGNORECASE))

    return has_copyright or has_spdx


def is_source_file(file_path: str) -> bool:
    """Check if file is a source code file that should be scanned."""
    # Skip generated files, dependencies, binaries FIRST
    skip_dirs = ['node_modules/', 'vendor/', 'dist/', 'build/', '.git/']
    if any(skip_dir in file_path for skip_dir in skip_dirs):
        return False

    # Skip test directories and test files (more specific patterns)
    lower_path = file_path.lower()
    test_patterns = ['/tests/', '/test/', '__tests__/', '__test__/']
    if any(pattern in lower_path for pattern in test_patterns):
        return False

    # Skip files that start with test_ (any language: test_foo.py, test_bar.js, test_baz.go)
    filename = file_path.split('/')[-1].lower()
    if filename.startswith('test_') or filename.startswith('test.'):
        return False

    # Check extension
    for ext in SOURCE_EXTENSIONS:
        if file_path.endswith(ext):
            return True

    # Check markdown files (scan code blocks)
    if file_path.endswith('.md'):
        return True

    return False


def is_new_file(file_diff: str) -> bool:
    """Check if file is newly added (not just modified)."""
    # New files have "new file mode" in diff
    return "new file mode" in file_diff


def analyze_with_claude(
    file_path: str,
    license_findings: Dict[str, List[str]],
    has_copyright: bool,
    is_new: bool,
    license_policy: str,
    usage_tracker: UsageTracker
) -> Dict[str, Any]:
    """
    Use Claude to analyze license findings against policy.

    Returns:
        dict with keys: status, explanation, suggestions
    """
    findings_summary = []

    if license_findings["prohibited"]:
        findings_summary.append(f"PROHIBITED licenses detected: {', '.join(license_findings['prohibited'])}")
    if license_findings["review_required"]:
        findings_summary.append(f"Review-required licenses detected: {', '.join(license_findings['review_required'])}")
    if license_findings["allowed"]:
        findings_summary.append(f"Allowed licenses detected: {', '.join(license_findings['allowed'])}")
    if is_new and not has_copyright:
        findings_summary.append("New file missing copyright header")

    if not findings_summary:
        findings_summary.append("No license text detected")

    prompt = f"""You are analyzing a source code file for license compliance according to our organizational policy.

FILE: {file_path}

LICENSE FINDINGS:
{chr(10).join(findings_summary)}

IS NEW FILE: {is_new}
HAS COPYRIGHT HEADER: {has_copyright}

LICENSE POLICY (from ADR-008):
{license_policy}

TASK:
Analyze the license findings against our policy and determine compliance status.

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks):
{{"status": "PASS|WARN|FAIL", "explanation": "brief explanation", "suggestions": ["suggestion 1", "suggestion 2"]}}

RULES:
- FAIL: Prohibited licenses (GPL, AGPL, SSPL) detected
- WARN: Review-required licenses (LGPL, MPL) OR new file missing copyright
- PASS: Only allowed licenses OR no license text detected

Examples:
- Prohibited: {{"status": "FAIL", "explanation": "GPL license detected - prohibited per ADR-008", "suggestions": ["Remove code with GPL license", "Replace with MIT/Apache licensed alternative"]}}
- Review needed: {{"status": "WARN", "explanation": "LGPL license requires legal review per ADR-008", "suggestions": ["Contact legal team for LGPL review"]}}
- Missing copyright: {{"status": "WARN", "explanation": "New file missing copyright header per ADR-008", "suggestions": ["Add copyright header: // Copyright 2024-2025 [Company Name]", "Add SPDX-License-Identifier: Apache-2.0"]}}
- Clean: {{"status": "PASS", "explanation": "No prohibited licenses detected", "suggestions": []}}"""

    logger.info(f"Calling Claude to analyze license compliance for {file_path}...")

    try:
        output, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            max_tokens=1000,
            temperature=0.1
        )
        usage_tracker.add(usage)

        # Parse JSON response
        # Remove markdown code blocks if present
        output = output.strip()
        if output.startswith("```"):
            output = output.split("```")[1]
            if output.startswith("json"):
                output = output[4:]
        output = output.strip()

        result = json.loads(output)

        return {
            "status": result.get("status", "WARN"),
            "explanation": result.get("explanation", "License analysis completed"),
            "suggestions": result.get("suggestions", [])
        }

    except Exception as e:
        logger.error(f"Claude analysis failed: {e}")
        # Fallback to rule-based decision
        if license_findings["prohibited"]:
            return {
                "status": "FAIL",
                "explanation": f"Prohibited licenses detected: {', '.join(license_findings['prohibited'])}",
                "suggestions": ["Remove code with prohibited license"]
            }
        elif license_findings["review_required"] or (is_new and not has_copyright):
            return {
                "status": "WARN",
                "explanation": "License requires review or missing copyright header",
                "suggestions": ["Review license compliance with legal team"]
            }
        else:
            return {
                "status": "PASS",
                "explanation": "No prohibited licenses detected",
                "suggestions": []
            }


def analyze_file_comprehensive(
    file_path: str,
    file_diff: str,
    license_policy: str,
    is_ai_generated: bool,
    usage_tracker: UsageTracker
) -> Dict[str, Any]:
    """
    Comprehensive file analysis: Phase 1 (license headers) + Phase 2 (code similarity).

    Returns combined analysis with status and explanation.
    """
    # Phase 1: License header detection (always run)
    license_findings = detect_license_patterns(file_diff)
    is_new = is_new_file(file_diff)
    has_copyright = check_copyright_header(file_diff)

    phase1_analysis = analyze_with_claude(
        file_path,
        license_findings,
        has_copyright,
        is_new,
        license_policy,
        usage_tracker
    )

    # Phase 2: Code similarity detection (only for AI-generated code)
    if is_ai_generated:
        # Extract added code from diff
        added_lines = [
            line[1:] for line in file_diff.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        ]
        code = "\n".join(added_lines)

        # Phase 2.1: Quick pattern check
        pattern_match = quick_pattern_check(code)

        if pattern_match:
            # Phase 2.2: Claude semantic analysis
            logger.info(f"Pattern match for {file_path}, running semantic analysis...")
            similarity_analysis = analyze_code_similarity_with_claude(code, file_path, pattern_match, usage_tracker)

            # Check for HIGH/MEDIUM confidence matches to GPL/AGPL/SSPL
            gpl_matches = [
                p for p in similarity_analysis.get("similar_projects", [])
                if p.get("license", "").startswith(("GPL", "AGPL", "SSPL"))
                and p.get("confidence") in ["HIGH", "MEDIUM"]
            ]

            if gpl_matches:
                # Found GPL similarity - override to WARN (advisory only)
                top_match = gpl_matches[0]
                return {
                    "status": "WARN",
                    "explanation": f"Code similarity detected: {top_match['similarity_percentage']}% similar to {top_match['name']} ({top_match['license']}) - {top_match['confidence']} confidence",
                    "phase2_triggered": True,
                    "similar_projects": gpl_matches
                }
        else:
            logger.info(f"No suspicious patterns in {file_path}, skipping semantic analysis")

    # Return Phase 1 analysis if no Phase 2 concerns
    return {
        "status": phase1_analysis["status"],
        "explanation": phase1_analysis["explanation"],
        "phase2_triggered": False
    }


def check_license_compliance(
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
    License Compliance check handler.

    Phase 1: Scans source files for license patterns and copyright headers.
    Phase 2: Detects code similarity to copyrighted OSS (AI-generated code only).

    Provider-aware: dispatches on ``provider`` for per-file diff fetch
    (main scan) AND the PR commit-list inspection used to detect
    AI-generated code (Phase 2 activation). Both surfaces work on
    GitHub and Azure DevOps.
    """
    if provider not in ("github", "azure_devops", "gitlab"):
        raise NotImplementedError(
            f"check_license_compliance does not support provider={provider!r}."
        )

    logger.info(
        f"Running license compliance check for PR #{pr_number} "
        f"(provider={provider})"
    )

    # Create usage tracker
    usage_tracker = UsageTracker()

    # Provider-specific auth + PR context prep.
    github_token: Optional[str] = None
    ado_access_token: Optional[str] = None
    gitlab_access_token: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None

    if provider == "github":
        github_token = get_github_token(github_token_param)
    elif provider == "azure_devops":
        from shared.pr_metadata import fetch_pr_metadata

        ado_access_token = refresh_ado_token(connection_id)
        pr = fetch_pr_metadata(
            "azure_devops", pr_number,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
        )
        base_sha = pr.base_sha
        head_sha = pr.head_sha
    else:  # gitlab
        from shared.pr_metadata import fetch_pr_metadata
        from .gitlab_context import get_gitlab_token

        # ``repository`` IS the full project path (variable-depth slug).
        gitlab_access_token = get_gitlab_token(connection_id)
        pr = fetch_pr_metadata(
            "gitlab", pr_number,
            gitlab_project_path=repository,
            gitlab_access_token=gitlab_access_token,
        )
        base_sha = pr.base_sha
        head_sha = pr.head_sha

    # Resolve per-check retrieval scope. LICENSE_COMPLIANCE is a
    # "standards" check: walks the repo's workspace(s) + all
    # is_global workspaces. Fail-soft: falls back to
    # workspace-blind on any error.
    scope_workspace_ids: Optional[List[str]] = None
    try:
        from shared.pr_check_scope import resolve_pr_check_workspace_scope
        scope_workspace_ids = resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name=repository,
        )
    except Exception as scope_exc:
        logger.warning(
            f"Could not resolve workspace scope for LICENSE_COMPLIANCE: "
            f"{scope_exc}. Falling back to workspace-blind retrieval."
        )

    # Query knowledge base for license policy
    logger.info("Querying KB for license compliance policy (ADR-008)...")
    kb_response = query_knowledge_base(
        query_kb_lambda_name,
        "What are our open source license compliance policies? Which licenses are prohibited, allowed, or require review?",
        top_k=3,
        workspace_ids=scope_workspace_ids,
    )
    license_policy = kb_response.get("answer", "")
    logger.info(f"License policy from KB: {license_policy[:200]}...")

    # Filter to source files
    source_files = [f for f in changed_files if is_source_file(f)]

    if not source_files:
        logger.info("No source files to check for license compliance")
        return {
            "checkType": check_type,
            "status": "PASS",
            "message": "No source files to check",
            "details": [],
            "usage": usage_tracker
        }

    # AI-generated-code detection (Phase 2 activation) via PR commit-list
    # inspection. Works on both providers now -- see
    # shared.pr_commits.fetch_pr_commits.
    if provider == "github":
        ai_generated_files = get_ai_generated_files(
            repository, pr_number, source_files, github_token,
        )
    else:  # azure_devops or gitlab
        ai_generated_files = get_ai_generated_files(
            repository, pr_number, source_files, None,
            provider=provider,
            ado_org=ado_org, ado_project=ado_project, ado_repo=ado_repo,
            ado_access_token=ado_access_token,
            gitlab_access_token=gitlab_access_token,
        )
    logger.info(f"Scanning {len(source_files)} source files ({len(ai_generated_files)} AI-generated)")

    # Analyze each file
    file_analyses = {}  # Group files by issue type
    overall_status = "PASS"
    phase2_count = 0

    for file_path in source_files:
        try:
            # Provider-dispatched per-file diff fetch.
            file_diff = _fetch_license_file_diff(
                provider=provider,
                file_path=file_path,
                repository=repository,
                pr_number=pr_number,
                github_token=github_token,
                ado_org=ado_org,
                ado_project=ado_project,
                ado_repo=ado_repo,
                ado_access_token=ado_access_token,
                gitlab_access_token=gitlab_access_token,
                base_sha=base_sha,
                head_sha=head_sha,
            )

            # Comprehensive analysis (Phase 1 + Phase 2)
            is_ai_generated = file_path in ai_generated_files
            analysis = analyze_file_comprehensive(
                file_path,
                file_diff,
                license_policy,
                is_ai_generated,
                usage_tracker
            )

            if analysis.get("phase2_triggered"):
                phase2_count += 1

            # Track overall status (FAIL > WARN > PASS)
            if analysis["status"] == "FAIL":
                overall_status = "FAIL"
            elif analysis["status"] == "WARN" and overall_status != "FAIL":
                overall_status = "WARN"

            # Group files by explanation (issue type)
            explanation = analysis['explanation']
            if explanation not in file_analyses:
                file_analyses[explanation] = []
            file_analyses[explanation].append(file_path)

            logger.info(f"File {file_path}: {analysis['status']} - {analysis['explanation']}")

        except Exception as e:
            logger.error(f"Error analyzing {file_path}: {e}")
            error_msg = f"Error during analysis - {str(e)}"
            if error_msg not in file_analyses:
                file_analyses[error_msg] = []
            file_analyses[error_msg].append(file_path)

    # Build grouped output
    file_results = []
    for explanation, files in sorted(file_analyses.items()):
        # Add explanation header
        file_results.append(f"\n**{explanation}**")
        # Add files under this issue
        for file_path in files:
            file_results.append(f"- `{file_path}`")

    # Build final message
    if overall_status == "FAIL":
        fail_count = sum(len(files) for exp, files in file_analyses.items() if 'prohibited' in exp.lower() or 'gpl' in exp.lower())
        message = f"Prohibited licenses detected in {fail_count} file(s)"
    elif overall_status == "WARN":
        similarity_msg = f" ({phase2_count} code similarity warnings)" if phase2_count > 0 else ""
        message = f"License review required for {len(source_files)} file(s){similarity_msg}"
    else:
        message = f"All {len(source_files)} file(s) passed license compliance"

    return {
        "checkType": check_type,
        "status": overall_status,
        "message": message,
        "details": file_results,
        "usage": usage_tracker
    }
