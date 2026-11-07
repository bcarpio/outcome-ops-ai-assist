"""
Test Coverage check handler.

Language-agnostic test coverage validation that:
1. Fetches .outcomeops.yaml from the repository for explicit configuration
2. Queries the customer's knowledge base for their testing ADRs/standards
3. Falls back to backend-specific defaults based on code_generation.backend
4. Validates that new source files have corresponding test files
5. Checks against ALL test files in the repo (not just changed files)

Priority order for configuration:
1. Explicit patterns in .outcomeops.yaml testing.require_tests / testing.exclude_from_tests
2. Legacy .outcomeops.yaml testing.source_patterns / testing.test_patterns (backwards compat)
3. Backend-specific defaults based on .outcomeops.yaml code_generation.backend
4. Skip check if no configuration found (PASS with warning)
"""

import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

import boto3
import requests
import yaml
from botocore.exceptions import ClientError

from .bedrock_utils import UsageTracker, BedrockUsage, HAIKU_MODEL_ID
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


@dataclass
class TestRequirement:
    """Configuration for a source file pattern that requires tests."""
    pattern: str
    test_pattern: str
    description: str = ""


@dataclass
class TestExclusion:
    """Configuration for a source file pattern excluded from test requirements."""
    pattern: str
    reason: str = ""


@dataclass
class TestingConfig:
    """Parsed testing configuration from .outcomeops.yaml."""
    require_tests: List[TestRequirement] = field(default_factory=list)
    exclude_from_tests: List[TestExclusion] = field(default_factory=list)
    test_patterns: List[str] = field(default_factory=list)
    naming_convention: str = "Tests"
    # Legacy fields for backwards compatibility
    source_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)

# Backend-specific default patterns
# Each backend defines: source_patterns, test_patterns, name_extractor
BACKEND_DEFAULTS = {
    "python": {
        "source_patterns": ["lambda/*/handler.py", "src/**/*.py"],
        "test_patterns": ["**/test_*.py", "**/*_test.py", "**/tests/**/*.py"],
        "exclude_patterns": ["**/tests/**", "**/__pycache__/**", "**/venv/**"],
        "name_extractor": lambda path: path.split("/")[1] if path.startswith("lambda/") and len(path.split("/")) >= 2 else path.rsplit("/", 1)[-1].replace(".py", ""),
    },
    "java": {
        "source_patterns": ["src/main/java/**/*.java"],
        "test_patterns": ["src/test/java/**/*Test.java", "src/test/java/**/*Tests.java"],
        "exclude_patterns": ["**/test/**", "**/tests/**"],
        "name_extractor": lambda path: path.rsplit("/", 1)[-1].replace(".java", ""),
    },
    "typescript": {
        "source_patterns": ["src/**/*.ts", "src/**/*.tsx"],
        "test_patterns": ["**/*.test.ts", "**/*.spec.ts", "**/*.test.tsx", "**/*.spec.tsx", "**/tests/**/*.ts"],
        "exclude_patterns": ["**/*.test.ts", "**/*.spec.ts", "**/*.d.ts", "**/node_modules/**"],
        "name_extractor": lambda path: path.rsplit("/", 1)[-1].replace(".tsx", "").replace(".ts", ""),
    },
    "go": {
        "source_patterns": ["**/*.go"],
        "test_patterns": ["**/*_test.go"],
        "exclude_patterns": ["**/*_test.go", "**/vendor/**"],
        "name_extractor": lambda path: path.rsplit("/", 1)[-1].replace(".go", ""),
    },
}


def get_github_token(github_token_param: str) -> str:
    """Fetch GitHub token from SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(Name=github_token_param, WithDecryption=True)
        return response["Parameter"]["Value"]
    except ClientError as e:
        logger.error(f"Failed to get GitHub token: {e}")
        raise


def fetch_outcomeops_yaml(
    repository: str,
    token: str,
    *,
    ref: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fetch .outcomeops.yaml from the repository.

    Args:
        repository: Repository in format "owner/repo"
        token: GitHub personal access token
        ref: git ref (SHA, branch, or tag) to read the file at. When
            None, GitHub defaults to the repo's default branch — but
            callers driving PR-check config should pass the PR head
            SHA so a PR's config changes take effect on the same PR.

    Returns:
        Parsed YAML dict or None if not found
    """
    url = f"{_github_api()}/repos/{repository}/contents/.outcomeops.yaml"
    if ref:
        url = f"{url}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            logger.info(
                f"No .outcomeops.yaml found in {repository}"
                + (f" at ref {ref}" if ref else "")
            )
            return None
        response.raise_for_status()
        return yaml.safe_load(response.text)
    except Exception as e:
        logger.warning(f"Failed to fetch .outcomeops.yaml from {repository}: {e}")
        return None


def fetch_repo_test_files(repository: str, token: str, test_patterns: List[str]) -> List[str]:
    """
    Fetch all test files from the repository using GitHub's Git Tree API.

    This is essential because we need to check if test files exist for source files,
    not just if test files were changed in the PR.

    Args:
        repository: Repository in format "owner/repo"
        token: GitHub personal access token
        test_patterns: List of glob patterns for test files

    Returns:
        List of test file paths in the repository
    """
    # First get the default branch's SHA
    url = f"{_github_api()}/repos/{repository}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        default_branch = response.json().get("default_branch", "main")

        # Get the tree recursively
        tree_url = f"{_github_api()}/repos/{repository}/git/trees/{default_branch}?recursive=1"
        tree_response = requests.get(tree_url, headers=headers, timeout=30)
        tree_response.raise_for_status()

        tree_data = tree_response.json()
        all_files = [item["path"] for item in tree_data.get("tree", []) if item["type"] == "blob"]

        # Filter to only test files
        test_files = [f for f in all_files if matches_pattern(f, test_patterns)]
        logger.info(f"Found {len(test_files)} test files in {repository}")
        return test_files

    except Exception as e:
        logger.warning(f"Failed to fetch repo test files: {e}")
        return []


def parse_testing_config(raw_config: Dict[str, Any], backend: Optional[str] = None) -> TestingConfig:
    """
    Parse the testing section of .outcomeops.yaml into a TestingConfig.

    Supports both new format (require_tests/exclude_from_tests) and legacy format
    (source_patterns/test_patterns).

    Args:
        raw_config: The raw YAML dict
        backend: Optional backend type for defaults

    Returns:
        Parsed TestingConfig
    """
    testing = raw_config.get("testing", {})
    config = TestingConfig()

    # Parse new format: require_tests
    if "require_tests" in testing:
        for item in testing["require_tests"]:
            config.require_tests.append(TestRequirement(
                pattern=item.get("pattern", ""),
                test_pattern=item.get("test_pattern", ""),
                description=item.get("description", "")
            ))

    # Parse new format: exclude_from_tests
    if "exclude_from_tests" in testing:
        for item in testing["exclude_from_tests"]:
            config.exclude_from_tests.append(TestExclusion(
                pattern=item.get("pattern", ""),
                reason=item.get("reason", "")
            ))

    # Parse test_patterns
    config.test_patterns = testing.get("test_patterns", [])
    config.naming_convention = testing.get("naming_convention", "Tests")

    # Parse legacy format for backwards compatibility
    config.source_patterns = testing.get("source_patterns", [])
    config.exclude_patterns = testing.get("exclude_patterns", [])

    # If no test_patterns specified but using new format, derive from backend defaults
    if not config.test_patterns and backend and backend in BACKEND_DEFAULTS:
        config.test_patterns = BACKEND_DEFAULTS[backend]["test_patterns"]

    return config


def is_file_excluded(file_path: str, exclusions: List[TestExclusion]) -> Tuple[bool, str]:
    """
    Check if a file is excluded from test requirements.

    Args:
        file_path: Path to check
        exclusions: List of exclusion patterns

    Returns:
        Tuple of (is_excluded, reason)
    """
    for exclusion in exclusions:
        if matches_pattern(file_path, [exclusion.pattern]):
            return True, exclusion.reason
    return False, ""


def file_requires_tests(file_path: str, requirements: List[TestRequirement]) -> Tuple[bool, Optional[TestRequirement]]:
    """
    Check if a file matches any test requirement patterns.

    Args:
        file_path: Path to check
        requirements: List of test requirements

    Returns:
        Tuple of (requires_tests, matching_requirement or None)
    """
    for req in requirements:
        if matches_pattern(file_path, [req.pattern]):
            return True, req
    return False, None


def query_testing_standards(
    query_kb_lambda_name: str,
    repository: str,
    scope: str = "local",
    workspace_ids: Optional[List[str]] = None,
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """
    Query knowledge base for testing standards.

    Standard Hierarchy (ADR-007: Documentation-Driven Decision Making):
    1. Local standards (repo-specific) - Override global defaults
    2. Global standards (organization-wide ADRs) - Defaults

    Args:
        query_kb_lambda_name: Lambda function name for KB queries
        repository: Repository in format "owner/repo"
        scope: "local" for repo-specific or "global" for organization-wide

    Returns:
        Tuple of (standards_text, adr_reference or None, usage_dict)
    """
    # Build query based on scope
    if scope == "local":
        query = (
            f"What are the testing standards specifically for the {repository} repository? "
            f"Where should test files be located in {repository}? "
            "Look for repo-specific testing documentation or ADRs."
        )
    else:
        query = (
            "What are the global Lambda handler testing standards? "
            "Where should test files be located according to organization-wide ADRs? "
            "What ADR defines our testing policy?"
        )

    payload: Dict[str, Any] = {
        "query": query,
        "topK": 5,  # Increase to capture both local and global docs
    }
    if workspace_ids:
        payload["workspaceIds"] = workspace_ids

    try:
        response = lambda_client.invoke(
            FunctionName=query_kb_lambda_name,
            Payload=json.dumps(payload)
        )

        # Parse Lambda response envelope
        response_payload = json.loads(response["Payload"].read())

        # query-kb returns {statusCode: 200, body: JSON_STRING}
        status_code = response_payload.get("statusCode", 500)
        if status_code != 200:
            logger.warning(f"KB query returned status {status_code}")
            return "", None, {}

        # Parse the body JSON string
        body = json.loads(response_payload.get("body", "{}"))
        answer = body.get("answer", "")

        # Extract usage from KB response if available
        # query-kb passes through usage from ask-claude with snake_case keys
        usage = body.get("usage", {})

        # Try to extract ADR reference from the answer
        adr_match = re.search(r'\b(ADR[-_]?\d+)\b', answer, re.IGNORECASE)
        adr_reference = adr_match.group(1).upper().replace("_", "-") if adr_match else None

        logger.info(f"Testing standards ({scope}) from KB (ADR: {adr_reference}): {answer[:200]}...")
        return answer, adr_reference, usage

    except Exception as e:
        logger.error(f"Failed to query testing standards ({scope}): {e}")
        return "", None, {}


def matches_pattern(path: str, patterns: List[str]) -> bool:
    """Check if a path matches any of the glob patterns."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Handle ** patterns by trying multiple approaches
        if "**" in pattern:
            # Try with ** matching any path (including empty)
            # e.g., **/test_*.py should match test_hello.py and foo/test_hello.py
            regex_pattern = pattern.replace(".", r"\.")
            regex_pattern = regex_pattern.replace("**/", "(.*/)?" )
            regex_pattern = regex_pattern.replace("**", ".*")
            regex_pattern = regex_pattern.replace("*", "[^/]*")
            regex_pattern = f"^{regex_pattern}$"
            if re.match(regex_pattern, path):
                return True
    return False


def normalize_name(name: str) -> str:
    """Normalize a name for comparison (lowercase, hyphens to underscores)."""
    return name.lower().replace("-", "_")


def find_source_files(
    changed_files: List[str],
    source_patterns: List[str],
    exclude_patterns: List[str]
) -> List[str]:
    """Find source files that match patterns and aren't excluded."""
    source_files = []
    for f in changed_files:
        if matches_pattern(f, source_patterns):
            if not matches_pattern(f, exclude_patterns):
                source_files.append(f)
    return source_files


def find_test_files(changed_files: List[str], test_patterns: List[str]) -> List[str]:
    """Find test files that match patterns."""
    return [f for f in changed_files if matches_pattern(f, test_patterns)]


def extract_name(path: str, backend: str) -> str:
    """Extract the meaningful name from a source file path for matching."""
    if backend in BACKEND_DEFAULTS:
        extractor = BACKEND_DEFAULTS[backend]["name_extractor"]
        return extractor(path)
    # Fallback: use filename without extension
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def check_test_coverage(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    github_token_param: str = "",
    query_kb_lambda_name: str = "",
    *,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    anchors: Optional[Dict[str, List[int]]] = None,
) -> Dict[str, Any]:
    """
    Language-agnostic test coverage check.

    Priority for configuration:
    1. Explicit .outcomeops.yaml testing.require_tests / testing.exclude_from_tests (new format)
    2. Legacy .outcomeops.yaml testing.source_patterns / testing.test_patterns
    3. Backend-specific defaults from .outcomeops.yaml code_generation.backend
    4. PASS with warning if no configuration found

    Key improvement: Checks against ALL test files in the repo, not just changed files.

    Provider-aware kwargs (``provider``, ``connection_id``, ``ado_org``,
    ``ado_project``, ``ado_repo``) exist for Azure DevOps parity; real
    ADO execution ships in a subsequent chunk.

    Args:
        check_type: The check type identifier
        pr_number: Pull request number
        repository: Repository in format "owner/repo"
        changed_files: List of changed file paths
        github_token_param: SSM parameter name for GitHub token
        query_kb_lambda_name: Lambda function name for KB queries

    Returns:
        Check result dict with status, message, and details
    """
    if provider not in ("github", "azure_devops", "gitlab"):
        raise NotImplementedError(
            f"check_test_coverage does not support provider={provider!r}."
        )

    logger.info(
        f"Running test coverage check for PR #{pr_number} in {repository} "
        f"(provider={provider})"
    )

    # Default to environment variables if not provided
    if not github_token_param:
        environment = os.environ.get("ENVIRONMENT", "dev")
        app_name = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
        github_token_param = f"/{environment}/{app_name}/github/token"

    if not query_kb_lambda_name:
        environment = os.environ.get("ENVIRONMENT", "dev")
        app_name = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
        query_kb_lambda_name = f"{environment}-{app_name}-query-kb"

    # Fetch repository configuration + default branch (provider-dispatched)
    raw_config = None
    backend = None
    token = None
    repo_default_branch: Optional[str] = None
    repo_file_paths: Optional[List[str]] = None

    # Fetch .outcomeops.yaml at the PR HEAD (not the default branch)
    # so a PR that adds/edits config sees that config applied to itself
    # -- matching ADR_COMPLIANCE and the other source-reading checks
    # which all read PR-head content, not main. Prior to this the check
    # skipped with "no testing configuration found" even when the PR
    # branch introduced a valid .outcomeops.yaml; only merges to main
    # took effect. Governance note: this means a PR can (in principle)
    # weaken its own coverage rules; treat that as a code-review
    # concern rather than a technical guard here.
    from shared.pr_metadata import fetch_pr_metadata, PRMetadataError
    from shared.source_fetch import fetch_file_at_sha, SourceFetchError

    if provider == "github":
        try:
            token = get_github_token(github_token_param)
            try:
                pr_meta = fetch_pr_metadata(
                    "github", pr_number,
                    repository=repository, github_token=token,
                )
                raw_config = fetch_outcomeops_yaml(
                    repository, token, ref=pr_meta.head_sha,
                )
            except PRMetadataError as e:
                logger.warning(
                    f"Could not fetch PR metadata for {repository}#{pr_number}: {e}. "
                    "Falling back to default branch for .outcomeops.yaml."
                )
                raw_config = fetch_outcomeops_yaml(repository, token)
        except Exception as e:
            logger.warning(f"Could not fetch .outcomeops.yaml: {e}")
    else:  # azure_devops or gitlab
        from shared.repo_metadata import fetch_repo_metadata, RepoMetadataError

        # Per-provider kwargs for the shared fetchers. GitLab's project
        # path IS ``repository`` (variable-depth slug); its ref slot
        # accepts a commit SHA or branch name uniformly, so no
        # version_type flag is needed there.
        try:
            if provider == "azure_devops":
                access_token = refresh_ado_token(connection_id)
                fetch_kwargs = dict(
                    ado_org=ado_org, ado_project=ado_project,
                    ado_repo=ado_repo, ado_access_token=access_token,
                )
            else:  # gitlab
                from .gitlab_context import get_gitlab_token, get_gitlab_base_url
                access_token = get_gitlab_token(connection_id)
                gitlab_base = get_gitlab_base_url(connection_id)
                fetch_kwargs = dict(
                    gitlab_project_path=repository,
                    gitlab_access_token=access_token,
                    gitlab_base_url=gitlab_base,
                )
            repo_meta = fetch_repo_metadata(provider, **fetch_kwargs)
            repo_default_branch = repo_meta.default_branch
            repo_file_paths = repo_meta.file_paths

            # Read .outcomeops.yaml at the PR HEAD (see note above --
            # parity with GitHub side, matches every other check's
            # source-fetch semantics). Fall back to the default branch
            # only if PR-metadata retrieval fails.
            try:
                pr_meta = fetch_pr_metadata(provider, pr_number, **fetch_kwargs)
                yaml_ref = pr_meta.head_sha
                yaml_version_type = "commit"
            except PRMetadataError as e:
                logger.warning(
                    f"Could not fetch {provider} PR metadata for #{pr_number}: {e}. "
                    "Falling back to default branch for .outcomeops.yaml."
                )
                yaml_ref = repo_default_branch
                yaml_version_type = "branch"

            try:
                yaml_bytes = fetch_file_at_sha(
                    provider, ".outcomeops.yaml",
                    yaml_ref,
                    ado_version_type=yaml_version_type,
                    **fetch_kwargs,
                )
                raw_config = yaml.safe_load(yaml_bytes.decode("utf-8", errors="replace"))
            except SourceFetchError:
                logger.info(
                    f"No .outcomeops.yaml at {yaml_version_type} {yaml_ref} "
                    f"in {provider} repo {repository}"
                )
        except RepoMetadataError as e:
            logger.warning(f"Could not fetch {provider} repo metadata: {e}")

    # Create usage tracker (TEST_COVERAGE uses Haiku via KB queries)
    usage_tracker = UsageTracker()

    # Query KB for BOTH local and global testing standards (ADR-007: Documentation-Driven Decisions)
    # Standard Hierarchy:
    # 1. Local standards (repo-specific) - Override global defaults
    # 2. Global standards (organization-wide ADRs) - Defaults
    local_standards, local_adr_reference = "", None
    global_standards, global_adr_reference = "", None

    # Resolve per-check retrieval scope. TEST_COVERAGE is a
    # "standards" check: walks the repo's workspace(s) + all
    # is_global workspaces. Fail-soft: retrieval falls back to
    # workspace-blind on error, matching pre-fix behavior.
    scope_workspace_ids: Optional[List[str]] = None
    try:
        from shared.pr_check_scope import resolve_pr_check_workspace_scope
        scope_workspace_ids = resolve_pr_check_workspace_scope(
            check_type=check_type, repo_name=repository,
        )
    except Exception as scope_exc:
        logger.warning(
            f"Could not resolve workspace scope for TEST_COVERAGE: "
            f"{scope_exc}. Falling back to workspace-blind retrieval."
        )

    try:
        # Query local repo-specific standards first
        local_standards, local_adr_reference, local_usage = query_testing_standards(
            query_kb_lambda_name, repository, scope="local",
            workspace_ids=scope_workspace_ids,
        )
        if local_usage:
            # query-kb returns usage with snake_case keys: input_tokens, output_tokens, cost_usd
            usage_tracker.add(BedrockUsage(
                model_id=HAIKU_MODEL_ID,
                input_tokens=local_usage.get("input_tokens", 0),
                output_tokens=local_usage.get("output_tokens", 0)
            ))
        logger.info(f"Retrieved local testing standards for {repository}")
    except Exception as e:
        logger.warning(f"Could not query local testing standards: {e}")

    try:
        # Query global organization-wide standards
        global_standards, global_adr_reference, global_usage = query_testing_standards(
            query_kb_lambda_name, repository, scope="global",
            workspace_ids=scope_workspace_ids,
        )
        if global_usage:
            # query-kb returns usage with snake_case keys: input_tokens, output_tokens, cost_usd
            usage_tracker.add(BedrockUsage(
                model_id=HAIKU_MODEL_ID,
                input_tokens=global_usage.get("input_tokens", 0),
                output_tokens=global_usage.get("output_tokens", 0)
            ))
        logger.info("Retrieved global testing standards from ADRs")
    except Exception as e:
        logger.warning(f"Could not query global testing standards: {e}")

    # Use local ADR reference if available, fall back to global
    adr_reference = local_adr_reference or global_adr_reference
    # Combine standards for pattern matching (local takes precedence)
    testing_standards = local_standards or global_standards

    # Determine backend
    if raw_config:
        backend = raw_config.get("code_generation", {}).get("backend")

    # Parse testing configuration
    testing_config = None
    config_source = "none"
    use_new_format = False

    if raw_config:
        testing_section = raw_config.get("testing", {})

        # Check if using new format (require_tests / exclude_from_tests)
        if testing_section.get("require_tests") or testing_section.get("exclude_from_tests"):
            testing_config = parse_testing_config(raw_config, backend)
            config_source = ".outcomeops.yaml (require_tests/exclude_from_tests)"
            use_new_format = True
            logger.info(f"Using new testing config format from .outcomeops.yaml")

        # Check for legacy format (source_patterns / test_patterns)
        elif testing_section.get("source_patterns") and testing_section.get("test_patterns"):
            testing_config = parse_testing_config(raw_config, backend)
            config_source = ".outcomeops.yaml (legacy source_patterns)"
            logger.info(f"Using legacy patterns from .outcomeops.yaml")

        # Fall back to backend-specific defaults
        elif backend and backend in BACKEND_DEFAULTS:
            testing_config = TestingConfig(
                source_patterns=BACKEND_DEFAULTS[backend]["source_patterns"],
                test_patterns=BACKEND_DEFAULTS[backend]["test_patterns"],
                exclude_patterns=BACKEND_DEFAULTS[backend].get("exclude_patterns", [])
            )
            config_source = f"backend defaults ({backend})"
            logger.info(f"Using {backend} backend defaults")

    # No configuration found - return WARN. The check couldn't
    # actually run, so treating this as PASS is misleading (and the
    # emoji glyph on the comment then contradicted the "skipped"
    # message body). The status glyph is supplied by
    # ``format_check_comment``; the message must not start with its
    # own emoji or the comment shows two glyphs stacked.
    if not testing_config:
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "Test coverage check skipped - no testing configuration found",
            "details": [
                "No `.outcomeops.yaml` found or no testing configuration.",
                "Add testing configuration to enable this check:",
                "```yaml",
                "testing:",
                "  require_tests:",
                "    - pattern: 'src/**/*Controller.java'",
                "      test_pattern: 'src/test/**/{name}Tests.java'",
                "  exclude_from_tests:",
                "    - pattern: 'src/**/*Repository.java'",
                "      reason: 'Tested via integration tests'",
                "```",
                "Or set `code_generation.backend` to use language defaults (python, java, typescript, go)."
            ],
            # The check could not run (no config), so there is nothing to
            # anchor. See the final return for why findings is file-level-only.
            "findings": [],
            "usage": usage_tracker
        }

    # Determine which test patterns to use
    test_patterns = testing_config.test_patterns
    if not test_patterns and backend and backend in BACKEND_DEFAULTS:
        test_patterns = BACKEND_DEFAULTS[backend]["test_patterns"]

    # Fetch ALL test files from the repository (not just changed files)
    all_test_files = []
    if test_patterns:
        if provider == "github":
            if token:
                all_test_files = fetch_repo_test_files(repository, token, test_patterns)
        else:  # azure_devops or gitlab (repo_file_paths from fetch_repo_metadata)
            if repo_file_paths is not None:
                all_test_files = [
                    f for f in repo_file_paths
                    if matches_pattern(f, test_patterns)
                ]
        logger.info(f"Fetched {len(all_test_files)} test files from repository")

    # Also include test files from changed files (in case they're being added in this PR)
    changed_test_files = find_test_files(changed_files, test_patterns)
    all_test_files = list(set(all_test_files + changed_test_files))

    logger.info(f"Total test files (repo + PR): {len(all_test_files)}")

    # Process changed files based on config format
    suggestions = []
    skipped_files = []
    # Missing-test source files paired with the requiring pattern (when
    # readily available). Used below to emit one line-anchored Finding per
    # file via the caller-supplied ``anchors`` map (shared.pr_findings).
    missing_test_files: List[Tuple[str, Optional[str]]] = []

    if use_new_format:
        # New format: use require_tests and exclude_from_tests
        for source_file in changed_files:
            # Skip non-source files (tests, configs, etc.)
            if matches_pattern(source_file, test_patterns):
                continue

            # Check if file is excluded from test requirements
            is_excluded, reason = is_file_excluded(source_file, testing_config.exclude_from_tests)
            if is_excluded:
                skipped_files.append(f"{source_file}: {reason}")
                logger.info(f"Skipping {source_file}: {reason}")
                continue

            # Check if file requires tests
            requires_tests, requirement = file_requires_tests(source_file, testing_config.require_tests)
            if not requires_tests:
                # File doesn't match any require_tests pattern - skip it
                continue

            # File requires tests - check if corresponding test exists
            source_name = extract_name(source_file, backend or "")
            source_name_normalized = normalize_name(source_name)

            has_test = any(
                source_name_normalized in normalize_name(test_file)
                for test_file in all_test_files
            )

            if not has_test:
                desc = requirement.description if requirement else "Testing standards require tests"
                if adr_reference:
                    desc = f"{adr_reference}: {desc}"

                missing_test_files.append(
                    (source_file, requirement.pattern if requirement else None)
                )
                suggestions.append(
                    f"**{source_file}**: Missing test file\n"
                    f"  - {desc}\n"
                    f"  - Add a test file that includes `{source_name}` in the name\n"
                    f"  - Config source: {config_source}"
                )
    else:
        # Legacy format: use source_patterns / exclude_patterns
        source_patterns = testing_config.source_patterns
        exclude_patterns = testing_config.exclude_patterns

        if not source_patterns and backend and backend in BACKEND_DEFAULTS:
            source_patterns = BACKEND_DEFAULTS[backend]["source_patterns"]
            exclude_patterns = BACKEND_DEFAULTS[backend].get("exclude_patterns", [])

        source_files = find_source_files(changed_files, source_patterns, exclude_patterns)

        logger.info(f"Found {len(source_files)} source files in changed files")
        logger.info(f"Source files: {source_files}")

        for source_file in source_files:
            source_name = extract_name(source_file, backend or "")
            source_name_normalized = normalize_name(source_name)

            has_test = any(
                source_name_normalized in normalize_name(test_file)
                for test_file in all_test_files
            )

            if not has_test:
                if adr_reference:
                    adr_msg = f"{adr_reference} requires tests for new code"
                else:
                    adr_msg = "Project testing standards require tests for new code"

                missing_test_files.append((source_file, None))
                suggestions.append(
                    f"**{source_file}**: Missing test file\n"
                    f"  - {adr_msg}\n"
                    f"  - Add a test file that includes `{source_name}` in the name\n"
                    f"  - Config source: {config_source}"
                )

    # Determine final status. The status glyph is supplied by
    # ``format_check_comment``; message must NOT start with its own
    # emoji or the comment shows two glyphs stacked.
    if not suggestions:
        if skipped_files:
            message = f"Test coverage OK ({len(skipped_files)} file(s) excluded per config)"
        elif adr_reference:
            message = f"All source files have test coverage per {adr_reference}"
        else:
            message = "All source files have test coverage"
        status = "PASS"
        details = suggestions
    else:
        status = "FAIL"
        violation_ref = f"({adr_reference} violation)" if adr_reference else "(testing standards violation)"
        message = f"{len(suggestions)} source file(s) missing required tests {violation_ref}"

        # Build details with suggestions and standards context
        details = suggestions.copy()

        # Show both local and global standards if we have missing tests
        if local_standards:
            details.append("")
            details.append("**Local testing standards (repo-specific):**")
            # First 5 lines of local standards
            local_lines = local_standards.split("\n")[:5]
            details.append("\n".join(local_lines))

        if global_standards:
            details.append("")
            details.append("**Global testing standards (organization-wide ADRs):**")
            # First 5 lines of global standards
            global_lines = global_standards.split("\n")[:5]
            details.append("\n".join(global_lines))

        # Add compliance note if both standards exist
        if local_standards and global_standards:
            details.append("")
            details.append(
                "**Note:** This repo may use local standards that differ from global ADRs. "
                "Check compliance against both standards."
            )

    # Line-anchored findings for inline PR comments (shared.pr_findings /
    # ADR-031). check_test_coverage is a FILE-LEVEL check: per changed file it
    # decides whether that file requires tests (file_requires_tests) and lacks
    # them (is_file_excluded). The caller now supplies ``anchors`` -- a map of
    # repo-relative path -> 1-based first added (RIGHT-side) line in the PR
    # diff -- so each missing-test file can be anchored to the new untested
    # code rather than left file-level-only. We emit ONE Finding per
    # missing-test source file that appears in ``anchors``. A file that is not
    # in ``anchors`` (anchor computation failed, or the file has no added
    # lines) is NOT fabricated a line -- ADR-031 forbids guessing -- so it
    # emits no Finding and stays in the <details> summary above. Severity
    # tracks the overall check status (error on FAIL, warning otherwise);
    # enclosing_symbol stays None (never guessed, per ADR-031).
    from shared.pr_findings import Finding

    findings: List[Dict[str, Any]] = []
    anchor_map = anchors or {}
    finding_severity = "error" if status == "FAIL" else "warning"
    for source_file, pattern in missing_test_files:
        # TEST_COVERAGE emits one Finding per missing-test file, so the file's
        # first added line is the right anchor (``anchors`` is now the full
        # added-line list per file; take the first).
        anchor_lines = anchor_map.get(source_file)
        anchor_line = anchor_lines[0] if anchor_lines else None
        if anchor_line is None:
            # No safe line anchor -- leave this file in the summary only.
            continue
        if pattern:
            message = (
                f"This file requires tests (pattern `{pattern}`) but none "
                "were found in the PR."
            )
        else:
            message = "This file requires tests but none were found in the PR."
        findings.append(
            Finding(
                path=source_file,
                line=anchor_line,
                side="RIGHT",
                severity=finding_severity,
                message=message,
                enclosing_symbol=None,
            ).model_dump()
        )

    return {
        "checkType": check_type,
        "status": status,
        "message": message,
        "details": details,
        "findings": findings,
        "usage": usage_tracker
    }
