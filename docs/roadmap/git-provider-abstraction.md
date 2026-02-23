# Git Provider Abstraction Layer

**Status:** Planned
**Priority:** Medium
**Depends On:** ADR-011 (API Gateway Exposure and Git Providers)

## Overview

Create a common abstraction layer for Git providers to support GitHub Cloud, GitHub Enterprise, GitLab Cloud, and GitLab Self-Managed.

## Current State

All Lambdas are tightly coupled to GitHub Cloud:
- `generate-code`: GitHub webhook handler, GitHub API for branches/commits/PRs
- `generate-code-maps`: GitHub API for cloning repos, reading files
- `run-tests`: GitHub API for cloning, posting PR comments

## Proposed Abstraction

```python
# lambda/shared/git_provider.py
class GitProvider(ABC):
    @abstractmethod
    def clone_repo(self, repo_full_name: str, branch: str) -> Path

    @abstractmethod
    def create_branch(self, repo_full_name: str, branch_name: str, base_branch: str)

    @abstractmethod
    def commit_file(self, repo_full_name: str, branch: str, path: str, content: str, message: str)

    @abstractmethod
    def create_pull_request(self, repo_full_name: str, title: str, body: str, head: str, base: str)

    @abstractmethod
    def post_pr_comment(self, repo_full_name: str, pr_number: int, comment: str)

    @abstractmethod
    def verify_webhook_signature(self, payload: bytes, signature: str, secret: str) -> bool

    @abstractmethod
    def get_file_contents(self, repo_full_name: str, path: str, branch: str) -> str

class GitHubProvider(GitProvider):
    """GitHub Cloud and Enterprise implementation"""
    pass

class GitLabProvider(GitProvider):
    """GitLab Cloud and Self-Managed implementation"""
    pass
```

## Lambdas Requiring Updates

| Lambda | Changes Required |
|--------|------------------|
| generate-code | Webhook validation, branch/commit/PR APIs |
| generate-code-maps | Clone repos, read files, list repos |
| run-tests | Clone repos, post PR comments |

## Configuration

Provider selected via environment variable or SSM parameter:

```hcl
variable "git_provider" {
  type = string
  # github_cloud, github_enterprise, gitlab_cloud, gitlab_self_managed
}
```

## Implementation Phases

1. **Phase 1**: Extract GitHub code into GitHubProvider class (refactor only, no new features)
2. **Phase 2**: Add GitLabProvider implementation
3. **Phase 3**: Add Enterprise variants (custom base URLs, different auth)

## Not In Scope (First Client)

First client needs:
- GitHub Cloud (public)
- Jira integration (already completed via SSM)

GitLab and GitHub Enterprise support can wait for customer demand.

## Related

- ADR-011: API Gateway Exposure and Git Providers
- Roadmap: Jira Integration (completed)
