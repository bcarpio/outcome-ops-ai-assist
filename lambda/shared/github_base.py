"""GitHub base-URL resolution — GHES (GitHub Enterprise Server) support.

Every GitHub call site derives its URLs from these helpers instead of
hardcoding the SaaS hosts. The deploy-wide knob is the ``GITHUB_BASE_URL``
env var (tfvar ``github_base_url``); unset/empty means github.com and every
helper returns the exact SaaS values the codebase used before this module
existed — byte-identical behavior for existing deploys.

GHES URL shapes are NOT a plain hostname swap:

- REST API lives under ``https://{host}/api/v3`` (SaaS: ``api.github.com``).
  This includes the GitHub App auth endpoints (``/app/installations/...``) —
  on GHES the App is registered ON the instance and its tokens mint against
  the instance's own ``/api/v3``.
- There is no ``raw.githubusercontent.com`` equivalent host. Raw file
  content on GHES is served through the contents API with
  ``Accept: application/vnd.github.raw`` (see shared.source_fetch, which
  keeps the raw host on SaaS to preserve its rate-limit posture).
- Git-over-HTTPS clone URLs use the instance host directly.

Values are read from the environment at call time (cheap string ops), so
tests can monkeypatch ``GITHUB_BASE_URL`` without reimporting modules.
Callers that cache the result in a module constant are fine too — the env
is static for the life of a deployed Lambda.
"""

from __future__ import annotations

import os

_SAAS_BASE = "https://github.com"
_SAAS_API = "https://api.github.com"
_SAAS_RAW = "https://raw.githubusercontent.com"


def github_base_url() -> str:
    """The instance base URL, e.g. ``https://github.com`` (default) or
    ``https://github.example.com`` for GHES."""
    return (os.environ.get("GITHUB_BASE_URL") or _SAAS_BASE).rstrip("/")


def is_saas() -> bool:
    """True when targeting github.com (the default)."""
    return github_base_url() == _SAAS_BASE


def github_api_base() -> str:
    """REST API root: ``https://api.github.com`` on SaaS,
    ``{base}/api/v3`` on GHES."""
    base = github_base_url()
    return _SAAS_API if base == _SAAS_BASE else f"{base}/api/v3"


def github_raw_base() -> str:
    """Raw-content host for SaaS ONLY. GHES callers must not use this —
    route through the contents API instead (shared.source_fetch does).
    Kept as a helper so the one SaaS raw URL shape lives here too."""
    return _SAAS_RAW


def github_host() -> str:
    """Bare host (netloc) for git-over-HTTPS clone URLs."""
    return github_base_url().split("://", 1)[-1]
