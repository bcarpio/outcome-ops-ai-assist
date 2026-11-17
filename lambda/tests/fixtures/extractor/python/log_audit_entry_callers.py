"""Fixture: the log_audit_entry v1 success case.

Mirrors the actual platform pattern where 9 integration handlers + the
scheduled-reporter all import and call
``log_audit_entry(active_repos=...)``. The extractor must surface
this as 10 ``calls`` Facts so the symbol-graph reader's ``callers``
query can identify every caller.
"""

from audit import log_audit_entry


def confluence_connect():
    active = ["confluence:ABC", "confluence:DEF"]
    log_audit_entry(active_repos=active)


def github_connect():
    active = ["github:owner/repo"]
    log_audit_entry(active_repos=active)


def jira_connect():
    active = ["jira:PROJ"]
    log_audit_entry(active_repos=active)


def outlook_connect():
    active = ["outlook:conn-1"]
    log_audit_entry(active_repos=active)


def sharepoint_connect():
    active = ["sharepoint:site-1"]
    log_audit_entry(active_repos=active)


def teams_connect():
    active = ["teams:team-1/channel-1"]
    log_audit_entry(active_repos=active)


def onedrive_connect():
    active = ["onedrive:drive-1"]
    log_audit_entry(active_repos=active)


def onenote_connect():
    active = ["onenote:user-1"]
    log_audit_entry(active_repos=active)


def database_connect():
    active = ["database:mssql:host-hash"]
    log_audit_entry(active_repos=active)


def scheduled_reporter():
    active = ["github:owner/repo"]
    log_audit_entry(active_repos=active)
