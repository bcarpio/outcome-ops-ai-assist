# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared.gitlab_instances — the multi-instance registry.

The invariant that protects every existing deploy: with GITLAB_INSTANCES
unset, behaviour is identical to the old single-instance env vars.
"""

import json

import pytest

from shared import gitlab_instances as gi


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GITLAB_INSTANCES", "GITLAB_BASE_URL", "GITLAB_CLIENT_ID",
                "GITLAB_CLIENT_SECRET_SSM_PARAM"):
        monkeypatch.delenv(var, raising=False)


class TestLegacyBackCompat:
    """A deploy that never sets GITLAB_INSTANCES must be untouched."""

    def test_bare_defaults_to_saas(self):
        [inst] = gi.list_instances()
        assert inst.id == gi.DEFAULT_INSTANCE_ID
        assert inst.base_url == "https://gitlab.com"
        assert inst.display_name == "GitLab.com"

    def test_legacy_vars_are_honored(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com/")
        monkeypatch.setenv("GITLAB_CLIENT_ID", "abc123")
        monkeypatch.setenv("GITLAB_CLIENT_SECRET_SSM_PARAM", "/prd/x/secret")
        [inst] = gi.list_instances()
        assert inst.base_url == "https://gitlab.example.com"  # trailing / stripped
        assert inst.client_id == "abc123"
        assert inst.client_secret_ssm_param == "/prd/x/secret"
        # self-managed gets its host as the label rather than "GitLab.com"
        assert inst.display_name == "https://gitlab.example.com"

    def test_empty_list_falls_back_to_legacy(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", "[]")
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.com")
        [inst] = gi.list_instances()
        assert inst.id == gi.DEFAULT_INSTANCE_ID

    def test_not_multi_instance_by_default(self):
        assert gi.is_multi_instance() is False


def _two_instances():
    return json.dumps([
        {"id": "saas", "display_name": "GitLab.com",
         "base_url": "https://gitlab.com",
         "client_id": "saas-id", "client_secret_ssm_param": "/p/saas"},
        {"id": "internal", "display_name": "GitLab (internal)",
         "base_url": "https://gitlab.example.com/",
         "client_id": "int-id", "client_secret_ssm_param": "/p/int"},
    ])


class TestMultiInstance:

    def test_parses_list_in_order(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", _two_instances())
        ids = [i.id for i in gi.list_instances()]
        assert ids == ["saas", "internal"]
        assert gi.is_multi_instance() is True

    def test_resolve_by_id(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", _two_instances())
        inst = gi.get_instance("internal")
        assert inst.base_url == "https://gitlab.example.com"  # normalized
        assert inst.client_id == "int-id"

    def test_unknown_id_raises_and_names_the_options(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", _two_instances())
        with pytest.raises(gi.GitLabInstanceError) as e:
            gi.get_instance("nope")
        assert "nope" in str(e.value)
        assert "saas" in str(e.value) and "internal" in str(e.value)

    def test_display_name_defaults_to_id(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps(
            [{"id": "only", "base_url": "https://g.example.com"}]))
        [inst] = gi.list_instances()
        assert inst.display_name == "only"

    def test_duplicate_ids_rejected(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps([
            {"id": "dup", "base_url": "https://a.example.com"},
            {"id": "dup", "base_url": "https://b.example.com"},
        ]))
        with pytest.raises(gi.GitLabInstanceError, match="duplicate"):
            gi.list_instances()


class TestMalformedConfigIsLoud:
    """Silently defaulting would mint tokens against the wrong GitLab."""

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", "{not json")
        with pytest.raises(gi.GitLabInstanceError, match="not valid JSON"):
            gi.list_instances()

    def test_non_list_raises(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", '{"id":"x"}')
        with pytest.raises(gi.GitLabInstanceError, match="must be a JSON list"):
            gi.list_instances()

    def test_missing_id_raises(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps(
            [{"base_url": "https://g.example.com"}]))
        with pytest.raises(gi.GitLabInstanceError, match="missing 'id'"):
            gi.list_instances()

    def test_missing_base_url_raises(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps([{"id": "x"}]))
        with pytest.raises(gi.GitLabInstanceError, match="missing 'base_url'"):
            gi.list_instances()


class TestLegacyConnectionRows:
    """Rows written before this feature carry no instance_id."""

    def test_blank_resolves_when_single_instance(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.com")
        assert gi.get_instance(None).id == gi.DEFAULT_INSTANCE_ID
        assert gi.get_instance("").id == gi.DEFAULT_INSTANCE_ID

    def test_blank_raises_when_ambiguous(self, monkeypatch):
        monkeypatch.setenv("GITLAB_INSTANCES", _two_instances())
        with pytest.raises(gi.GitLabInstanceError, match="cannot infer"):
            gi.get_instance(None)

    def test_default_id_resolves_to_the_single_configured_instance(self, monkeypatch):
        """A deploy that moved from legacy vars to a one-entry list must not
        orphan its existing 'default' connections."""
        monkeypatch.setenv("GITLAB_INSTANCES", json.dumps(
            [{"id": "saas", "base_url": "https://gitlab.com"}]))
        assert gi.get_instance(gi.DEFAULT_INSTANCE_ID).id == "saas"
