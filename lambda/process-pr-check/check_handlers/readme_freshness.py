# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
README Freshness check handler.

Checks:
- If infrastructure files changed, the documentation should be updated
- If new handlers were added, the documentation should cover them
- Uses Claude to judge whether the documentation changes are meaningful

"Documentation" is any README file in the repo plus the doc locations the
repo declares in ``.outcomeops.yaml`` (``ingestion.docs_paths`` and
``ingestion.standards_paths``); the infrastructure location comes from
``infrastructure.directory``. Repos without the file get the defaults
(``docs``, ``docs/adr``, ``terraform``).
"""

import json
import logging
from typing import Dict, List, Any, Optional, Set, Tuple

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
ssm_client = boto3.client("ssm")


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


def fetch_pr_diff(repository: str, pr_number: int, token: str) -> str:
    """Fetch the full unified diff of a GitHub PR."""
    url = f"{_github_api()}/repos/{repository}/pulls/{pr_number}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch PR diff: {e}")
        raise Exception(f"GitHub API error: {str(e)}")


def fetch_pr_file_diff(repository: str, pr_number: int, file_path: str, token: str) -> str:
    """Fetch a single file's diff from GitHub PR."""
    full_diff = fetch_pr_diff(repository, pr_number, token)
    file_diff = extract_file_diff_from_full_diff(full_diff, file_path)
    return file_diff if file_diff else f"diff --git a/{file_path} b/{file_path}\n(No diff available)"


def extract_file_diff_from_full_diff(full_diff: str, file_path: str) -> str:
    """Extract a specific file's diff section from the full PR diff."""
    lines = full_diff.split("\n")

    file_diff_lines = []
    capturing = False

    header = f"diff --git a/{file_path} b/{file_path}"
    for line in lines:
        if line == header:
            capturing = True
            file_diff_lines.append(line)
        elif capturing and line.startswith("diff --git"):
            break
        elif capturing:
            file_diff_lines.append(line)

    return "\n".join(file_diff_lines) if file_diff_lines else ""


def extract_handler_names(files: List[str]) -> List[str]:
    """
    Extract handler names from file paths.

    Example: lambda/hello/handler.py -> "hello"
    """
    handlers = []

    for file in files:
        # Filter Lambda handler files (exclude tests and shared code)
        if (file.startswith("lambda/") and file.endswith("/handler.py") and "/tests/" not in file):
            parts = file.split("/")
            # Get handler directory name (e.g., "hello" from lambda/hello/handler.py)
            if len(parts) >= 3:
                handlers.append(parts[1])

    # Remove duplicates
    return list(set(handlers))


def extract_infrastructure_names(
    files: List[str], infra_dir: str = "terraform", infra_tool: str = "terraform",
) -> List[str]:
    """Names of changed infrastructure files: path under the infrastructure
    directory, without its extension (``terraform/vpc.tf`` -> ``vpc``)."""
    prefix = _normalize_dir(infra_dir)
    infra_names = []
    for file in files:
        if is_infrastructure_file(file, infra_dir, infra_tool):
            name = file[len(prefix) + 1:] if prefix else file
            infra_names.append(name.rsplit(".", 1)[0])
    return infra_names


# ============================================================================
# Documentation and infrastructure locations (from .outcomeops.yaml)
# ============================================================================

# Extensions treated as documentation inside a declared docs location.
_DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".adoc")

# Caps on how much documentation diff is sent to the model.
_MAX_DOC_DIFF_CHARS_PER_FILE = 8000
_MAX_DOC_DIFF_CHARS_TOTAL = 30000


def _normalize_dir(path: str) -> str:
    """``./docs/`` -> ``docs``; the repo root becomes ``""``."""
    path = (path or "").strip().strip("/")
    while path.startswith("./"):
        path = path[2:]
    return "" if path == "." else path


def _is_under(path: str, directory: str) -> bool:
    directory = _normalize_dir(directory)
    return directory == "" or path == directory or path.startswith(directory + "/")


def is_readme_file(path: str) -> bool:
    """A README file anywhere in the repo (``README``, ``README.md``, ...)."""
    name = path.rsplit("/", 1)[-1].lower()
    return name == "readme" or name.startswith("readme.")


def is_documentation_file(path: str, doc_dirs: List[str]) -> bool:
    """True for any README file, or a doc-extension file under a declared
    documentation directory."""
    if is_readme_file(path):
        return True
    if not path.lower().endswith(_DOC_EXTENSIONS):
        return False
    return any(_is_under(path, d) for d in doc_dirs)


def is_infrastructure_file(path: str, infra_dir: str, infra_tool: str = "terraform") -> bool:
    """A file under the infrastructure directory. For Terraform only ``.tf``
    files count (not lock files or tfvars examples); documentation files
    under the directory never count."""
    if not _normalize_dir(infra_dir) or not _is_under(path, infra_dir):
        return False
    if is_readme_file(path) or path.lower().endswith(_DOC_EXTENSIONS):
        return False
    if (infra_tool or "").lower() == "terraform":
        return path.endswith(".tf")
    return True


class _DocSettings:
    """Where this repo keeps documentation and infrastructure."""

    def __init__(self, doc_dirs: List[str], infra_dir: str, infra_tool: str):
        self.doc_dirs = doc_dirs
        self.infra_dir = infra_dir
        self.infra_tool = infra_tool

    @classmethod
    def from_repo_config(cls, config: Any) -> "_DocSettings":
        ingestion = config.ingestion
        doc_dirs = list(dict.fromkeys(
            list(ingestion.docs_paths) + list(ingestion.standards_paths)
        ))
        return cls(doc_dirs, config.infrastructure.directory, config.infrastructure.tool)

    def describe_locations(self) -> str:
        dirs = ", ".join(f"{_normalize_dir(d) or '(repo root)'}/" for d in self.doc_dirs)
        return f"README files or {dirs}" if dirs else "README files"


def analyze_readme_with_claude(
    readme_diff: str,
    handler_names: List[str],
    infra_names: List[str],
    usage_tracker: UsageTracker,
    workspace_id: str = ""
) -> Dict[str, Any]:
    """
    Ask Claude whether the documentation diff adequately documents the changes.

    ``readme_diff`` is the combined diff of every changed documentation file
    (README files and the declared doc directories), not only README.md.

    Returns:
        dict with keys: adequate (bool), explanation (str)
    """
    changes_summary = []
    if handler_names:
        changes_summary.append(f"- New/modified Lambda handlers: {', '.join(handler_names)}")
    if infra_names:
        changes_summary.append(f"- Infrastructure changes: {', '.join(infra_names)}")

    changes_text = "\n".join(changes_summary)

    prompt = f"""You are analyzing documentation updates in a pull request (README files and the repository's documentation directories) to determine if they adequately document the changes being made.

CHANGES IN THIS PR:
{changes_text}

DOCUMENTATION DIFF (what was added/removed, one section per file):
```diff
{readme_diff}
```

TASK:
Analyze ONLY the provided documentation diff above. Determine if it adequately documents the specific changes listed (new Lambda handlers and/or infrastructure changes).

IMPORTANT RULES:
1. ONLY use information from the documentation diff provided above
2. Do NOT make assumptions about what might be documented elsewhere
3. Do NOT hallucinate or invent documentation that isn't in the diff
4. Check if the specific handler names or infrastructure components are mentioned in the added lines (+ lines in diff)

RESPONSE FORMAT:
Respond with ONLY a JSON object (no markdown, no code blocks):
{{"adequate": true/false, "explanation": "brief explanation of what's documented or missing"}}

Examples:
- If handler "hello" is added to README: {{"adequate": true, "explanation": "README documents the new hello Lambda handler in the Lambda list and directory structure"}}
- If no handler mentioned: {{"adequate": false, "explanation": "README was modified but does not mention the new 'hello' Lambda handler"}}
- If only minor typo fix: {{"adequate": false, "explanation": "README changes appear unrelated to the new Lambda handler being added"}}"""

    logger.info("Calling Claude to analyze documentation adequacy...")

    try:
        response_text, usage = call_bedrock(
            prompt=prompt,
            model_id=SONNET_MODEL_ID,
            # Headroom for models that think before the JSON: at 500 a
            # Claude 5.x response was cut off mid-object and never parsed.
            max_tokens=2000,
            temperature=0.1,
            workspace_id=workspace_id
        )
        usage_tracker.add(usage)

        logger.info(f"Claude response: {response_text}")

        # Parse JSON response (handle markdown code blocks)
        json_text = response_text

        # Remove markdown code blocks if present
        json_text = json_text.replace("```json", "").replace("```", "").strip()

        # Extract JSON object if surrounded by other text
        if "{" in json_text and "}" in json_text:
            start = json_text.index("{")
            end = json_text.rindex("}") + 1
            json_text = json_text[start:end]

        try:
            parsed = json.loads(json_text)
            return {
                "adequate": parsed.get("adequate", False),
                "explanation": parsed.get("explanation", "No explanation provided")
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {response_text}")
            logger.error(f"Attempted to parse: {json_text}")
            logger.error(f"Parse error: {e}")

            # Never guess a verdict from fragments: a truncated
            # '{"adequate": false, ...' contains both "adequate" and "true"
            # only by accident, and a PASS here hides missing docs. Raise so
            # the check reports "could not verify" (manual review).
            raise ValueError("Could not parse documentation analysis response") from e

    except Exception as e:
        logger.error(f"Error calling Claude for README analysis: {e}")
        raise


class _PRSource:
    """Provider access for one PR: fetch files at the base or head commit.

    Built lazily, only when the check needs the repo config or a diff.
    """

    def __init__(
        self,
        *,
        provider: str,
        repository: str,
        pr_number: int,
        github_token_param: str,
        connection_id: Optional[str],
        ado_org: Optional[str],
        ado_project: Optional[str],
        ado_repo: Optional[str],
    ):
        from shared.pr_metadata import fetch_pr_metadata

        self.provider = provider
        self.repository = repository
        self.pr_number = pr_number
        self.github_token: Optional[str] = None

        if provider == "github":
            self.github_token = get_github_token(github_token_param)
            self.fetch_kwargs: Dict[str, Any] = {
                "repository": repository, "github_token": self.github_token,
            }
        elif provider == "azure_devops":
            access_token = refresh_ado_token(connection_id)
            self.fetch_kwargs = {
                "ado_org": ado_org, "ado_project": ado_project,
                "ado_repo": ado_repo, "ado_access_token": access_token,
            }
        elif provider == "gitlab":
            from .gitlab_context import get_gitlab_token, get_gitlab_base_url

            # ``repository`` IS the full project path (variable-depth slug).
            self.fetch_kwargs = {
                "gitlab_project_path": repository,
                "gitlab_access_token": get_gitlab_token(connection_id),
                "gitlab_base_url": get_gitlab_base_url(connection_id),
            }
        else:
            raise ValueError(f"Unknown provider: {provider}")

        meta = fetch_pr_metadata(provider, pr_number, **self.fetch_kwargs)
        self.base_sha = meta.base_sha
        self.head_sha = meta.head_sha

    def file_at(self, path: str, sha: str) -> Optional[bytes]:
        """File bytes at ``sha``, or None when the file is absent there."""
        from shared.source_fetch import fetch_file_at_sha, SourceFetchError

        try:
            return fetch_file_at_sha(self.provider, path, sha, **self.fetch_kwargs)
        except SourceFetchError:
            return None

    def file_diffs(self, paths: List[str]) -> Dict[str, str]:
        """Unified diff per path. GitHub uses its server-side PR diff (one
        request); the other providers diff base and head bytes locally."""
        if self.provider == "github":
            full_diff = fetch_pr_diff(self.repository, self.pr_number, self.github_token)
            return {
                p: d for p in paths
                if (d := extract_file_diff_from_full_diff(full_diff, p))
            }

        from shared.pr_diff import compute_file_diff

        diffs: Dict[str, str] = {}
        for path in paths:
            diff = compute_file_diff(
                self.file_at(path, self.base_sha),
                self.file_at(path, self.head_sha),
                path,
            )
            if diff.strip():
                diffs[path] = diff
        return diffs


def _load_doc_settings(source: Optional["_PRSource"]) -> "_DocSettings":
    """Documentation and infrastructure locations from ``.outcomeops.yaml``
    at the PR head, so a PR that moves the docs is judged by its own config.
    Missing, unreadable or invalid config falls back to the defaults."""
    from shared.repo_config import get_default_config, parse_repo_config

    config = get_default_config()
    if source is not None:
        raw = source.file_at(".outcomeops.yaml", source.head_sha)
        if raw is not None:
            try:
                config = parse_repo_config(raw.decode("utf-8", errors="replace"))
            except ValueError as e:
                logger.warning(
                    f"[readme_freshness] invalid .outcomeops.yaml, using "
                    f"default doc locations: {e}"
                )
    return _DocSettings.from_repo_config(config)


def _combine_doc_diffs(diffs: Dict[str, str]) -> str:
    """Join per-file doc diffs for the model, within the size caps."""
    parts: List[str] = []
    total = 0
    for path in sorted(diffs):
        diff = diffs[path]
        if len(diff) > _MAX_DOC_DIFF_CHARS_PER_FILE:
            diff = diff[:_MAX_DOC_DIFF_CHARS_PER_FILE] + "\n... (diff truncated)"
        if total + len(diff) > _MAX_DOC_DIFF_CHARS_TOTAL:
            parts.append(f"... ({len(diffs) - len(parts)} more documentation file(s) not shown)")
            break
        parts.append(diff)
        total += len(diff)
    return "\n".join(parts)


def check_readme_freshness(
    check_type: str,
    pr_number: int,
    repository: str,
    changed_files: List[str],
    github_token_param: str,
    *,
    provider: str = "github",
    connection_id: str | None = None,
    ado_org: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    new_files: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    README Freshness check handler.

    Checks:
    - If infrastructure files changed, the documentation should be updated
    - If new handlers were added, the documentation should cover them
    - Uses Claude to judge whether the documentation changes are meaningful

    Documentation is any README file plus the repo's declared doc locations,
    and infrastructure is the declared infrastructure directory, both read
    from ``.outcomeops.yaml`` at the PR head (defaults: ``docs``,
    ``docs/adr``, ``terraform``).

    ``new_files`` is the set of changed files absent at the PR base and
    present at head. When it is a set, only handlers in it count as new;
    edits to existing handlers do not require a documentation update. When
    it is ``None`` (newness unknown), any changed handler still triggers the
    check, but the message does not claim the handlers are new.
    """
    if provider not in ("github", "azure_devops", "gitlab"):
        raise NotImplementedError(
            f"check_readme_freshness does not support provider={provider!r}."
        )

    logger.info(
        f"Running README freshness check for PR #{pr_number} "
        f"(provider={provider})"
    )

    usage_tracker = UsageTracker()

    handler_files = [
        f for f in changed_files
        if f.startswith("lambda/") and f.endswith("/handler.py") and "/tests/" not in f
    ]
    newness_known = new_files is not None
    if newness_known:
        # Modified handlers are not a documentation trigger; only new ones are.
        relevant_handler_files = [f for f in handler_files if f in new_files]
    else:
        relevant_handler_files = handler_files

    # Provider access, needed for the repo config and the doc diffs. A
    # failure here falls back to default locations; the check still runs.
    source: Optional[_PRSource] = None
    try:
        source = _PRSource(
            provider=provider,
            repository=repository,
            pr_number=pr_number,
            github_token_param=github_token_param,
            connection_id=connection_id,
            ado_org=ado_org,
            ado_project=ado_project,
            ado_repo=ado_repo,
        )
    except Exception as e:
        logger.warning(
            f"[readme_freshness] could not read PR #{pr_number} sources, "
            f"using default doc locations: {e}"
        )
    settings = _load_doc_settings(source)

    infra_files = [
        f for f in changed_files
        if is_infrastructure_file(f, settings.infra_dir, settings.infra_tool)
    ]
    doc_files = [f for f in changed_files if is_documentation_file(f, settings.doc_dirs)]
    where = settings.describe_locations()

    if not infra_files and not relevant_handler_files:
        return {
            "checkType": check_type,
            "status": "PASS",
            "message": "No new handlers or infrastructure changes detected",
            "details": [],
            "usage": usage_tracker
        }

    if not doc_files:
        details = []
        if infra_files:
            details.append(
                f"Infrastructure files changed ({len(infra_files)} file(s)) but no "
                f"documentation was updated (looked in {where})."
            )
        if relevant_handler_files and newness_known:
            details.append(
                f"New Lambda handlers added ({len(relevant_handler_files)} file(s)) but no "
                f"documentation was updated (looked in {where})."
            )
        elif relevant_handler_files:
            details.append(
                f"Lambda handler files changed ({len(relevant_handler_files)} file(s)) but no "
                f"documentation was updated (looked in {where}); could not determine "
                "whether any are new."
            )
        details.append(
            "Consider documenting new resources, handlers, and their purposes. "
            "Documentation locations are set by ingestion.docs_paths in .outcomeops.yaml."
        )
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "Documentation not updated",
            "details": details,
            "usage": usage_tracker
        }

    logger.info(
        f"Documentation updated ({len(doc_files)} file(s)), analyzing adequacy with Claude..."
    )

    # Attribute PR-analyzer Bedrock spend to the PR repo's OWNING workspace
    # (the first repo-row owner for this provider). Fail-soft: "" -> WS#SYSTEM (unattributed)
    # rather than breaking the check.
    owning_ws = ""
    try:
        from shared.pr_check_scope import find_workspaces_for_repo
        owning_ws = (sorted(find_workspaces_for_repo(repository, provider=provider)) or [""])[0]
    except Exception as ws_exc:
        logger.warning(
            f"Could not resolve owning workspace for spend attribution: {ws_exc}"
        )

    try:
        if source is None:
            raise RuntimeError("PR sources unavailable")
        docs_diff = _combine_doc_diffs(source.file_diffs(doc_files))

        if not docs_diff.strip():
            logger.warning("Documentation files changed but no diff found")
            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "Documentation updated",
                "details": [
                    f"Documentation was modified ({', '.join(sorted(doc_files))}); "
                    "diff not available for analysis."
                ],
                "usage": usage_tracker
            }

        handler_names = extract_handler_names(relevant_handler_files)
        infra_names = extract_infrastructure_names(
            changed_files, settings.infra_dir, settings.infra_tool
        )

        analysis = analyze_readme_with_claude(
            docs_diff, handler_names, infra_names, usage_tracker, workspace_id=owning_ws
        )

        if analysis["adequate"]:
            return {
                "checkType": check_type,
                "status": "PASS",
                "message": "Documentation adequately covers changes",
                "details": [analysis["explanation"]],
                "usage": usage_tracker
            }
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "Documentation may not adequately cover changes",
            "details": [
                analysis["explanation"],
                "",
                "Suggested improvements:",
                "- Document each new Lambda handler where this repo keeps handler docs",
                "- Update directory structure diagrams",
                "- Document the purpose and usage of new components"
            ],
            "usage": usage_tracker
        }

    except Exception as e:
        logger.error(f"[readme_freshness] Error analyzing documentation: {e}", exc_info=True)
        return {
            "checkType": check_type,
            "status": "WARN",
            "message": "Documentation updated (could not verify adequacy)",
            "details": [
                "Documentation was modified, but automated analysis failed.",
                "Please manually verify the documentation is adequate.",
                f"Error: {str(e)}"
            ],
            "usage": usage_tracker
        }
