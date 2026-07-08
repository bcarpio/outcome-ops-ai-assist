"""Unit tests for shared/salesforce_render.py."""

import os
import sys

import pytest


_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.salesforce_render import render_record_markdown, source_for  # noqa: E402


def test_source_for_account():
    assert source_for("Account") == "salesforce_account"


def test_source_for_contact():
    assert source_for("Contact") == "salesforce_contact"


def test_source_for_opportunity():
    assert source_for("Opportunity") == "salesforce_opportunity"


class TestRenderAccount:
    def test_full_frontmatter_and_body(self):
        record = {
            "Id": "001XXX",
            "Name": "Acme Corp",
            "Type": "Customer - Direct",
            "Industry": "Technology",
            "Website": "https://acme.example",
            "BillingCity": "SF",
            "BillingState": "CA",
            "BillingCountry": "US",
            "NumberOfEmployees": 500,
            "AnnualRevenue": 10_000_000,
            "Owner": {"Name": "Alice"},
            "Description": "Great customer.",
            "SystemModstamp": "2026-06-01T12:00:00Z",
        }
        title, md = render_record_markdown(
            "Account", record, [], "https://acme.my.salesforce.com",
        )
        assert title == "Acme Corp"
        assert "object: Account" in md
        assert "name: Acme Corp" in md
        assert "location: SF, CA, US" in md
        assert "url: https://acme.my.salesforce.com/001XXX" in md
        assert "# Acme Corp" in md
        assert "Great customer." in md

    def test_activities_section_appended(self):
        record = {"Id": "001", "Name": "Acme", "SystemModstamp": "x"}
        activities = [
            {
                "_activity_kind": "Task",
                "ActivityDate": "2026-06-01",
                "Subject": "Follow up",
                "Description": "Call to discuss renewal.",
                "Owner": {"Name": "Alice"},
                "LastModifiedDate": "2026-06-01T14:00:00Z",
            },
        ]
        _, md = render_record_markdown("Account", record, activities, "")
        assert "## Recent activity" in md
        assert "Follow up" in md
        assert "Alice" in md


class TestRenderOpportunityNextStep:
    def test_next_step_in_body(self):
        record = {
            "Id": "006",
            "Name": "Big Deal",
            "SystemModstamp": "x",
            "NextStep": "Send proposal",
        }
        _, md = render_record_markdown("Opportunity", record, [], "")
        assert "**Next step:** Send proposal" in md


class TestNullSafety:
    def test_missing_owner_relation(self):
        record = {"Id": "003", "Name": "Bob", "SystemModstamp": "x"}
        title, md = render_record_markdown("Contact", record, [], "")
        # Should not raise; owner line simply absent
        assert "owner:" not in md
        assert title == "Bob"

    def test_falls_back_to_id_when_no_name(self):
        record = {"Id": "001XXX", "SystemModstamp": "x"}
        title, md = render_record_markdown("Account", record, [], "")
        assert title == "001XXX"

    def test_long_description_truncated_in_activity(self):
        record = {"Id": "001", "Name": "X", "SystemModstamp": "x"}
        activities = [
            {
                "_activity_kind": "Task",
                "ActivityDate": "2026-06-01",
                "Subject": "S",
                "Description": "x" * 1000,
                "LastModifiedDate": "2026-06-01T14:00:00Z",
            },
        ]
        _, md = render_record_markdown("Account", record, activities, "")
        # ACTIVITY_DESC_MAX = 500
        assert "…" in md
