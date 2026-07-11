"""Unit tests for shared.group_workspace_sync (Azure AD group -> workspace)."""

from __future__ import annotations

import os
import sys

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.group_workspace_sync import compute_delta, desired_memberships  # noqa: E402


USER = "alice@outcomeops.onmicrosoft.com"

# Admin-configured bindings: group -> workspace @ role.
BINDINGS = [
    {"group_id": "g-eng", "workspace_id": "ws-eng", "role": "member"},
    {"group_id": "g-eng-admin", "workspace_id": "ws-eng", "role": "workspace_admin"},
    {"group_id": "g-sales", "workspace_id": "ws-sales", "role": "member"},
]


def _member(ws, role="member", source=None):
    m = {"workspace_id": ws, "role": role, "user_email": USER}
    if source is not None:
        m["source"] = source
    return m


class TestDesiredMemberships:
    def test_only_groups_in_claim_count(self):
        d = desired_memberships(["g-eng"], BINDINGS)
        assert d == {"ws-eng": ("member", "g-eng")}

    def test_highest_role_wins_across_groups(self):
        # user is in both g-eng (member) and g-eng-admin (workspace_admin)
        d = desired_memberships(["g-eng", "g-eng-admin"], BINDINGS)
        assert d["ws-eng"][0] == "workspace_admin"

    def test_empty_claim_yields_nothing(self):
        assert desired_memberships([], BINDINGS) == {}


class TestComputeDelta:
    def test_new_group_grant_is_written(self):
        to_put, to_delete = compute_delta(USER, ["g-eng"], BINDINGS, existing_members=[])
        assert to_delete == []
        assert to_put == [
            {"workspace_id": "ws-eng", "role": "member",
             "source": "group:g-eng", "user_email": USER}
        ]

    def test_group_removed_revokes_group_owned_row(self):
        existing = [_member("ws-eng", source="group:g-eng")]
        to_put, to_delete = compute_delta(USER, claim_group_ids=[], bindings=BINDINGS,
                                          existing_members=existing)
        assert to_put == []
        assert to_delete == existing  # the group-owned row is revoked

    def test_manual_row_never_revoked(self):
        # user dropped from all groups, but has a MANUAL membership -> untouched
        existing = [_member("ws-eng", role="workspace_admin", source="manual")]
        to_put, to_delete = compute_delta(USER, [], BINDINGS, existing)
        assert to_put == [] and to_delete == []

    def test_legacy_row_without_source_treated_as_manual(self):
        # rows predating this feature have NO source field -> never revoked
        existing = [_member("ws-eng")]  # no source key at all
        to_put, to_delete = compute_delta(USER, [], BINDINGS, existing)
        assert to_put == [] and to_delete == []

    def test_manual_row_defers_group_grant(self):
        # user is in g-eng (would grant member) but already has a manual admin
        # row for ws-eng -> group sync leaves the manual row, writes nothing.
        existing = [_member("ws-eng", role="workspace_admin", source="manual")]
        to_put, to_delete = compute_delta(USER, ["g-eng"], BINDINGS, existing)
        assert to_put == [] and to_delete == []

    def test_role_drift_on_group_owned_row_rewrites(self):
        # group-owned row is 'member' but the user is now in the admin group
        existing = [_member("ws-eng", role="member", source="group:g-eng")]
        to_put, to_delete = compute_delta(
            USER, ["g-eng", "g-eng-admin"], BINDINGS, existing)
        assert to_delete == []
        assert to_put[0]["role"] == "workspace_admin"
        assert to_put[0]["source"] == "group:g-eng-admin"

    def test_steady_state_no_changes(self):
        existing = [_member("ws-eng", source="group:g-eng")]
        to_put, to_delete = compute_delta(USER, ["g-eng"], BINDINGS, existing)
        assert to_put == [] and to_delete == []

    def test_add_and_revoke_together(self):
        # in sales now (add), no longer in eng (revoke the group-owned eng row)
        existing = [_member("ws-eng", source="group:g-eng")]
        to_put, to_delete = compute_delta(USER, ["g-sales"], BINDINGS, existing)
        assert to_put == [
            {"workspace_id": "ws-sales", "role": "member",
             "source": "group:g-sales", "user_email": USER}
        ]
        assert to_delete == existing
