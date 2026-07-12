"""Azure AD group -> workspace membership resolution (chunk 11a).

Pure, side-effect-free logic. Given a user's current Entra group IDs (from the
OIDC ``groups`` claim), the admin-configured group->workspace bindings, and the
user's existing ``WS_MEMBER#`` rows, compute which membership rows to write and
which to revoke on login.

Ownership model -- the load-bearing invariant:
  * This sync OWNS only rows tagged ``source == "group:<gid>"``.
  * MANUAL memberships -- rows with NO ``source`` (legacy rows predate this
    feature) or ``source == "manual"`` -- are NEVER written or revoked here.
    The workspace-management admin endpoints own those.
  * There is exactly one WS_MEMBER# row per (user, workspace)
    (PK=USER#<email>, SK=WS_MEMBER#<workspace_id>), so where a manual row
    already grants a workspace the group sync defers to it entirely -- no
    duplicate row, no role downgrade.

Role vocabulary is the live handler's ``workspace_admin | member`` (NOT the
plan's aspirational viewer/editor/admin). When several of a user's groups bind
the same workspace, the highest role wins.
"""

from typing import Dict, Iterable, List, Tuple

GROUP_SOURCE_PREFIX = "group:"
MANUAL_SOURCE = "manual"

_ROLE_RANK = {"member": 0, "workspace_admin": 1}


def _is_group_owned(source: str) -> bool:
    return bool(source) and source.startswith(GROUP_SOURCE_PREFIX)


def _higher_role(a: str, b: str) -> str:
    return a if _ROLE_RANK.get(a, 0) >= _ROLE_RANK.get(b, 0) else b


def _binding_identifiers(b: Dict) -> List[str]:
    """The normalized values a binding can be matched by (ADR-048 v1.1).

    A hybrid Entra tenant emits a mix of object IDs and on-prem group names in
    one token's ``groups`` claim, and a binding may have been stored by either
    (object ID via the Graph picker, or on-prem name in the credential-free
    path). So a binding matches the claim on EITHER its ``group_id`` or its
    optional ``group_name``. GUIDs are case-insensitive and names are matched
    case-insensitively, so everything is compared lowercased.
    """
    out: List[str] = []
    for key in ("group_id", "group_name"):
        v = b.get(key)
        if v:
            out.append(v.strip().lower())
    return out


def desired_memberships(
    claim_group_ids: Iterable[str],
    bindings: Iterable[Dict],
) -> Dict[str, Tuple[str, str]]:
    """Return ``{workspace_id: (role, group_id)}`` the user should have.

    ``bindings`` are the admin-configured group->workspace rows; each is a dict
    with ``group_id``, ``workspace_id``, ``role``. Only bindings for groups the
    user is currently in count. If several of the user's groups bind the same
    workspace, the highest role wins and its ``group_id`` is recorded as the
    owning source.
    """
    claim = {g.strip().lower() for g in claim_group_ids if g}
    desired: Dict[str, Tuple[str, str]] = {}
    for b in bindings:
        gid = b.get("group_id")
        ws = b.get("workspace_id")
        role = b.get("role", "member")
        # Match by EITHER object ID or on-prem name (hybrid-tenant mixed claim).
        if not gid or not ws or not (set(_binding_identifiers(b)) & claim):
            continue
        if role not in _ROLE_RANK:
            role = "member"
        if ws in desired:
            role = _higher_role(desired[ws][0], role)
        # source stays keyed on the canonical group_id (object ID when known).
        desired[ws] = (role, gid)
    return desired


def compute_delta(
    user_email: str,
    claim_group_ids: Iterable[str],
    bindings: Iterable[Dict],
    existing_members: Iterable[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """Compute ``(to_put, to_delete)`` for a user's group-derived memberships.

    ``existing_members`` are the user's current WS_MEMBER# rows (dicts with at
    least ``workspace_id``, ``role``, and optionally ``source``).

    * ``to_put``: ``{workspace_id, role, source, user_email}`` rows to upsert --
      new group grants, or group-owned rows whose role/source drifted.
      Workspaces where a MANUAL row already exists are skipped (manual wins).
    * ``to_delete``: group-owned rows whose backing group is no longer in the
      claim, or whose binding was removed. Manual rows are never included.
    """
    desired = desired_memberships(claim_group_ids, bindings)
    existing_by_ws = {m["workspace_id"]: m for m in existing_members}

    to_put: List[Dict] = []
    for ws, (role, gid) in desired.items():
        cur = existing_by_ws.get(ws)
        source = f"{GROUP_SOURCE_PREFIX}{gid}"
        if cur is None:
            to_put.append({
                "workspace_id": ws, "role": role,
                "source": source, "user_email": user_email,
            })
        elif _is_group_owned(cur.get("source", "")):
            if cur.get("role") != role or cur.get("source") != source:
                to_put.append({
                    "workspace_id": ws, "role": role,
                    "source": source, "user_email": user_email,
                })
        # else: a manual row already grants this workspace -> leave it.

    to_delete: List[Dict] = []
    for ws, m in existing_by_ws.items():
        if _is_group_owned(m.get("source", "")) and ws not in desired:
            to_delete.append(m)
    return to_put, to_delete
