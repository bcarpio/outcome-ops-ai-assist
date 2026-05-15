"""Fixture: the report_repos_snapshot v1 success case.

Mirrors the actual platform pattern where 9 integration handlers + the
license-heartbeat-scheduler all import and call
``report_repos_snapshot(active_repos=...)``. The extractor must surface
this as 10 ``calls`` Facts so the symbol-graph reader's ``callers``
query can identify every caller.
"""

from outcomeops_license import report_repos_snapshot


def confluence_connect():
    active = ["confluence:ABC", "confluence:DEF"]
    report_repos_snapshot(active_repos=active)


def github_connect():
    active = ["github:owner/repo"]
    report_repos_snapshot(active_repos=active)


def jira_connect():
    active = ["jira:PROJ"]
    report_repos_snapshot(active_repos=active)


def outlook_connect():
    active = ["outlook:conn-1"]
    report_repos_snapshot(active_repos=active)


def sharepoint_connect():
    active = ["sharepoint:site-1"]
    report_repos_snapshot(active_repos=active)


def teams_connect():
    active = ["teams:team-1/channel-1"]
    report_repos_snapshot(active_repos=active)


def onedrive_connect():
    active = ["onedrive:drive-1"]
    report_repos_snapshot(active_repos=active)


def onenote_connect():
    active = ["onenote:user-1"]
    report_repos_snapshot(active_repos=active)


def database_connect():
    active = ["database:mssql:host-hash"]
    report_repos_snapshot(active_repos=active)


def heartbeat_scheduler():
    active = ["github:owner/repo"]
    report_repos_snapshot(active_repos=active)
