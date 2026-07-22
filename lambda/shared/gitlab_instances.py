"""GitLab instance registry — one deploy, many GitLab instances.

A deployment can connect gitlab.com AND one or more self-managed instances at
the same time. Real cases: an acquirer on gitlab.com absorbing a company that
runs self-managed, a months-long migration where both are live, or a regulated
org keeping OSS work on SaaS and regulated code on an internal instance.

The previous model was deploy-wide: ``GITLAB_BASE_URL`` / ``GITLAB_CLIENT_ID``
/ ``GITLAB_CLIENT_SECRET_SSM_PARAM`` as single env vars, so one deploy saw
exactly one GitLab and switching meant a terraform apply that broke every
existing connection.

Now: terraform serialises a LIST of instances into ``GITLAB_INSTANCES`` (JSON),
each connection records which instance it belongs to, and every API/token call
resolves config from the connection instead of the environment.

Back-compat is deliberate and load-bearing: a deploy that sets only the legacy
single-instance vars keeps working untouched — those synthesise one instance
with id ``default``, which is also the id assumed for pre-existing connection
rows that predate this feature. No migration, no reconnect.

Shape of ``GITLAB_INSTANCES``::

    [
      {"id": "saas",     "display_name": "GitLab.com",
       "base_url": "https://gitlab.com",
       "client_id": "...", "client_secret_ssm_param": "/prd/.../client-secret"},
      {"id": "internal", "display_name": "GitLab (internal)",
       "base_url": "https://gitlab.example.com",
       "client_id": "...", "client_secret_ssm_param": "/prd/.../client-secret-internal"}
    ]
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, NamedTuple, Optional

DEFAULT_INSTANCE_ID = "default"
_DEFAULT_BASE_URL = "https://gitlab.com"


class GitLabInstanceError(Exception):
    """Raised for unknown instance ids or malformed configuration.

    Deliberately loud: silently falling back to a different instance than the
    caller asked for would mint tokens against the wrong GitLab, or read a
    repo from the wrong one.
    """


class GitLabInstance(NamedTuple):
    """Everything needed to talk to one GitLab instance."""
    id: str
    display_name: str
    base_url: str
    client_id: str
    client_secret_ssm_param: str


def _normalize(raw: Dict, *, index: int) -> GitLabInstance:
    """Validate one raw entry. Missing id/base_url is a config bug, not a
    default-to-SaaS situation."""
    instance_id = (raw.get("id") or "").strip()
    if not instance_id:
        raise GitLabInstanceError(
            f"GITLAB_INSTANCES[{index}] is missing 'id'"
        )
    base = (raw.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise GitLabInstanceError(
            f"GITLAB_INSTANCES[{index}] ({instance_id}) is missing 'base_url'"
        )
    return GitLabInstance(
        id=instance_id,
        display_name=(raw.get("display_name") or "").strip() or instance_id,
        base_url=base,
        client_id=(raw.get("client_id") or "").strip(),
        client_secret_ssm_param=(raw.get("client_secret_ssm_param") or "").strip(),
    )


def _legacy_instance() -> GitLabInstance:
    """Synthesise the single-instance deploy from the legacy env vars."""
    base = (os.environ.get("GITLAB_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
    return GitLabInstance(
        id=DEFAULT_INSTANCE_ID,
        display_name="GitLab.com" if base == _DEFAULT_BASE_URL else base,
        base_url=base,
        client_id=os.environ.get("GITLAB_CLIENT_ID", "").strip(),
        client_secret_ssm_param=os.environ.get(
            "GITLAB_CLIENT_SECRET_SSM_PARAM", "").strip(),
    )


def list_instances() -> List[GitLabInstance]:
    """Every configured instance, in tfvars order.

    Falls back to the legacy single-instance vars when GITLAB_INSTANCES is
    unset or an empty list, so existing deploys behave identically.
    """
    raw = os.environ.get("GITLAB_INSTANCES", "").strip()
    if not raw:
        return [_legacy_instance()]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GitLabInstanceError(f"GITLAB_INSTANCES is not valid JSON: {e}")
    if not isinstance(parsed, list):
        raise GitLabInstanceError("GITLAB_INSTANCES must be a JSON list")
    if not parsed:
        return [_legacy_instance()]

    instances = [_normalize(entry, index=i) for i, entry in enumerate(parsed)]
    seen = set()
    for inst in instances:
        if inst.id in seen:
            raise GitLabInstanceError(
                f"GITLAB_INSTANCES has duplicate id {inst.id!r}"
            )
        seen.add(inst.id)
    return instances


def get_instance(instance_id: Optional[str]) -> GitLabInstance:
    """Resolve one instance by id.

    ``None``/empty means a connection row written before this feature existed;
    those belong to the single configured instance, so resolve to it when
    there is exactly one, and raise when the deploy has since become
    multi-instance (guessing would talk to the wrong GitLab).
    """
    instances = list_instances()
    wanted = (instance_id or "").strip()

    if not wanted:
        if len(instances) == 1:
            return instances[0]
        raise GitLabInstanceError(
            "connection has no instance_id and this deploy has "
            f"{len(instances)} GitLab instances configured; cannot infer "
            "which one it belongs to"
        )

    for inst in instances:
        if inst.id == wanted:
            return inst

    # A legacy 'default' connection on a deploy that has since been given an
    # explicit instance list: resolve to the single instance if unambiguous.
    if wanted == DEFAULT_INSTANCE_ID and len(instances) == 1:
        return instances[0]

    known = ", ".join(i.id for i in instances) or "<none>"
    raise GitLabInstanceError(
        f"unknown GitLab instance {wanted!r}; configured: {known}"
    )


def is_multi_instance() -> bool:
    """True when the deploy has more than one GitLab configured (drives the
    UI's instance picker and the enrollment collision check)."""
    return len(list_instances()) > 1
