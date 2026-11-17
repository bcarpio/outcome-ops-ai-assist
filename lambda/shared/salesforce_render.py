# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Render Salesforce records to markdown narrative documents.

A CRM record becomes searchable RAG knowledge when its descriptions,
next-steps, and recent activity timeline are rendered to a self-
contained markdown doc. The rendered markdown is passed to
file-ingestion in the SQS body; file-ingestion just writes it to S3
(no SOQL there).

Each object type maps to a ``source`` value consumed by file-ingestion:
    Account     -> salesforce_account
    Contact     -> salesforce_contact
    Opportunity -> salesforce_opportunity

Ported from ``../app.retrieveit.ai/lambda/salesforce-integration/
salesforce_render.py`` verbatim -- rendering logic is provider-agnostic
and has no OutcomeOps-vs-SaaS shape differences.
"""

from typing import Any, Dict, List, Optional, Tuple


OBJECT_SOURCE = {
    "Account": "salesforce_account",
    "Contact": "salesforce_contact",
    "Opportunity": "salesforce_opportunity",
}

# Activity description text can be long free-form notes; cap each line
# so the rendered doc (and the SQS message carrying it) stays under 256 KB.
ACTIVITY_DESC_MAX = 500


def source_for(object_type: str) -> str:
    return OBJECT_SOURCE[object_type]


def _rel(record: Dict[str, Any], rel: str, field: str) -> str:
    """Safely read a relationship field (e.g. Owner.Name) that may be null."""
    obj = record.get(rel)
    if isinstance(obj, dict):
        return str(obj.get(field) or "")
    return ""


def _fm_line(key: str, value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return f"{key}: {value}"


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _record_url(instance_url: str, record_id: str) -> str:
    base = (instance_url or "").rstrip("/")
    return f"{base}/{record_id}" if base and record_id else record_id


def _render_activities(activities: List[Dict[str, Any]]) -> List[str]:
    if not activities:
        return []
    lines = ["", "## Recent activity"]
    for a in activities:
        kind = a.get("_activity_kind", "Activity")
        date = a.get("ActivityDate") or a.get("ActivityDateTime") or ""
        subject = (a.get("Subject") or "").strip() or "(no subject)"
        desc = _truncate(a.get("Description") or "", ACTIVITY_DESC_MAX)
        owner = _rel(a, "Owner", "Name")
        head = f"- {date} — {kind} — {subject}".rstrip()
        if owner:
            head += f" ({owner})"
        if desc:
            head += f": {desc}"
        lines.append(head)
    return lines


def render_record_markdown(
    object_type: str,
    record: Dict[str, Any],
    activities: List[Dict[str, Any]],
    instance_url: str,
) -> Tuple[str, str]:
    """Render one record + its recent activities to (title, markdown)."""
    record_id = record.get("Id", "")
    name = (record.get("Name") or record.get("Subject") or "").strip() or record_id

    fm: List[str] = ["---", f"object: {object_type}"]

    common = [_fm_line("name", name)]
    if object_type == "Account":
        common += [
            _fm_line("type", record.get("Type")),
            _fm_line("industry", record.get("Industry")),
            _fm_line("website", record.get("Website")),
            _fm_line("employees", record.get("NumberOfEmployees")),
            _fm_line("annual_revenue", record.get("AnnualRevenue")),
            _fm_line(
                "location",
                ", ".join(
                    p for p in (
                        record.get("BillingCity"),
                        record.get("BillingState"),
                        record.get("BillingCountry"),
                    ) if p
                ) or None,
            ),
        ]
    elif object_type == "Contact":
        common += [
            _fm_line("title", record.get("Title")),
            _fm_line("department", record.get("Department")),
            _fm_line("account", _rel(record, "Account", "Name") or None),
            _fm_line("email", record.get("Email")),
        ]
    elif object_type == "Opportunity":
        common += [
            _fm_line("account", _rel(record, "Account", "Name") or None),
            _fm_line("stage", record.get("StageName")),
            _fm_line("amount", record.get("Amount")),
            _fm_line("close_date", record.get("CloseDate")),
            _fm_line("probability", record.get("Probability")),
            _fm_line("type", record.get("Type")),
            _fm_line("lead_source", record.get("LeadSource")),
        ]

    common += [
        _fm_line("owner", _rel(record, "Owner", "Name") or None),
        _fm_line("url", _record_url(instance_url, record_id)),
        _fm_line("last_modified", record.get("SystemModstamp")),
    ]
    fm += [line for line in common if line]
    fm.append("---")

    body: List[str] = ["", f"# {name}", ""]
    description = (record.get("Description") or "").strip()
    if description:
        body.append(description)
    if object_type == "Opportunity":
        next_step = (record.get("NextStep") or "").strip()
        if next_step:
            body += ["", f"**Next step:** {next_step}"]

    markdown = "\n".join(fm + body + _render_activities(activities))
    return name, markdown
