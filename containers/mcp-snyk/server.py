"""
Snyk MCP Server - Streamable HTTP Transport

Exposes Snyk security data via MCP tools by calling the Snyk REST API.
Unlike the official Snyk CLI MCP server (which needs local files), this
server queries existing scan results from Snyk's cloud API.

Authentication: SNYK_TOKEN environment variable (Snyk API token).
Transport: JSON-RPC 2.0 over HTTP POST (MCP Streamable HTTP).
"""

import json
import logging
import os
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("snyk-mcp")

PORT = int(os.environ.get("PORT", "8080"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp/snyk")
SNYK_TOKEN = os.environ.get("SNYK_TOKEN", "")
SNYK_ORG_ID = os.environ.get("SNYK_CFG_ORG", "")
SNYK_API_BASE = "https://api.snyk.io"
SNYK_V1_BASE = "https://snyk.io/api/v1"

# ============================================================================
# Snyk API Client
# ============================================================================


def _snyk_headers():
    return {
        "Authorization": f"token {SNYK_TOKEN}",
        "Content-Type": "application/json",
    }


def _snyk_rest_headers():
    return {
        "Authorization": f"token {SNYK_TOKEN}",
        "Content-Type": "application/vnd.api+json",
    }


def _snyk_request(url, headers=None):
    """Make a GET request to Snyk API."""
    hdrs = headers or _snyk_headers()
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        logger.error(f"Snyk API error {e.code}: {body[:500]}")
        raise Exception(f"Snyk API error {e.code}: {body[:200]}")


def _snyk_post(url, data, headers=None):
    """Make a POST request to Snyk API."""
    hdrs = headers or _snyk_headers()
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(), headers=hdrs, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        logger.error(f"Snyk API error {e.code}: {body[:500]}")
        raise Exception(f"Snyk API error {e.code}: {body[:200]}")


def _resolve_org_id(org_slug=None):
    """Resolve org ID from slug or use configured default."""
    if org_slug:
        # Try to find org by slug
        data = _snyk_request(f"{SNYK_V1_BASE}/orgs")
        for org in data.get("orgs", []):
            if org.get("slug") == org_slug or org.get("name") == org_slug:
                return org["id"]
        raise Exception(f"Organization not found: {org_slug}")
    if SNYK_ORG_ID:
        return SNYK_ORG_ID
    # Use first org
    data = _snyk_request(f"{SNYK_V1_BASE}/orgs")
    orgs = data.get("orgs", [])
    if orgs:
        return orgs[0]["id"]
    raise Exception("No Snyk organizations found")


# ============================================================================
# MCP Tool Definitions
# ============================================================================

MCP_TOOLS = [
    {
        "name": "list_organizations",
        "description": "List all Snyk organizations the authenticated user belongs to.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_projects",
        "description": (
            "List all monitored projects in a Snyk organization. "
            "Projects represent repositories, container images, or other targets being scanned. "
            "Use this to find project IDs for deeper analysis."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
            },
        },
    },
    {
        "name": "get_project_issues",
        "description": (
            "Get aggregated security issues (vulnerabilities and license problems) for a specific project. "
            "Returns issues grouped by severity with fix information. "
            "Use list_projects first to find the project ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The Snyk project ID (UUID).",
                },
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter by minimum severity: critical, high, medium, low.",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "type": {
                    "type": "string",
                    "description": "Filter by issue type: vuln or license.",
                    "enum": ["vuln", "license"],
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_project_dependencies",
        "description": (
            "Get the dependency graph for a project. Shows all direct and transitive "
            "dependencies with their versions. Useful for understanding the dependency tree "
            "and identifying where vulnerable packages are introduced."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The Snyk project ID (UUID).",
                },
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "search_issues_by_package",
        "description": (
            "Search for known vulnerabilities in a specific package by its Package URL (purl). "
            "Example purls: pkg:npm/express@4.17.1, pkg:pypi/django@4.2, pkg:maven/org.apache.log4j/log4j-core@2.17.0"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "purl": {
                    "type": "string",
                    "description": "Package URL (purl) to check for vulnerabilities.",
                },
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
            },
            "required": ["purl"],
        },
    },
    {
        "name": "get_org_issues",
        "description": (
            "List all security issues across all projects in an organization. "
            "Provides a high-level overview of the organization's security posture. "
            "Can filter by severity, issue type, and scan type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
                "severity": {
                    "type": "string",
                    "description": "Filter by severity: critical, high, medium, low.",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "scan_type": {
                    "type": "string",
                    "description": "Filter by scan type: sast (code), package (SCA/dependencies).",
                    "enum": ["sast", "package"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 100, max 1000).",
                },
            },
        },
    },
    {
        "name": "get_project_sbom",
        "description": (
            "Export the Software Bill of Materials (SBOM) for a project in CycloneDX or SPDX format. "
            "Returns a complete list of all components and dependencies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The Snyk project ID (UUID).",
                },
                "org": {
                    "type": "string",
                    "description": "Organization slug or ID. Defaults to configured org.",
                },
                "format": {
                    "type": "string",
                    "description": "SBOM format: cyclonedx+json (default) or spdx+json.",
                    "enum": ["cyclonedx+json", "spdx+json"],
                },
            },
            "required": ["project_id"],
        },
    },
]


# ============================================================================
# Tool Handlers
# ============================================================================


def handle_list_organizations(params):
    data = _snyk_request(f"{SNYK_V1_BASE}/orgs")
    orgs = data.get("orgs", [])
    lines = [f"Found {len(orgs)} organization(s):\n"]
    for org in orgs:
        lines.append(f"- **{org.get('name')}** (slug: {org.get('slug')}, id: {org.get('id')})")
    return "\n".join(lines)


def handle_list_projects(params):
    org_id = _resolve_org_id(params.get("org"))
    data = _snyk_request(f"{SNYK_V1_BASE}/org/{org_id}/projects")
    projects = data.get("projects", [])

    lines = [f"Found {len(projects)} project(s):\n"]
    for p in projects:
        status = p.get("status", "unknown")
        lines.append(
            f"- **{p.get('name')}** (type: {p.get('type')}, id: {p.get('id')}, status: {status})"
        )
    return "\n".join(lines)


def handle_get_project_issues(params):
    org_id = _resolve_org_id(params.get("org"))
    project_id = params["project_id"]

    filters = {}
    if params.get("severity"):
        sev_map = {"critical": ["critical"], "high": ["critical", "high"],
                    "medium": ["critical", "high", "medium"],
                    "low": ["critical", "high", "medium", "low"]}
        filters["severities"] = sev_map.get(params["severity"], ["critical", "high", "medium", "low"])
    if params.get("type"):
        filters["types"] = [params["type"]]
    if not filters.get("types"):
        filters["types"] = ["vuln", "license"]
    if not filters.get("severities"):
        filters["severities"] = ["critical", "high", "medium", "low"]

    data = _snyk_post(
        f"{SNYK_V1_BASE}/org/{org_id}/project/{project_id}/aggregated-issues",
        {"filters": filters},
    )
    issues = data.get("issues", [])

    # Count by severity
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in issues:
        sev = issue.get("issueData", {}).get("severity", "low")
        counts[sev] = counts.get(sev, 0) + 1

    lines = [
        f"Found {len(issues)} issue(s) for project {project_id}:\n",
        f"- Critical: {counts['critical']}",
        f"- High: {counts['high']}",
        f"- Medium: {counts['medium']}",
        f"- Low: {counts['low']}",
        "",
    ]

    for issue in issues[:50]:  # Limit output
        idata = issue.get("issueData", {})
        pkg_name = issue.get("pkgName", "unknown")
        pkg_ver = issue.get("pkgVersions", ["?"])[0] if issue.get("pkgVersions") else "?"
        title = idata.get("title", "Unknown")
        severity = idata.get("severity", "?")
        cve = ", ".join(idata.get("identifiers", {}).get("CVE", [])) or "N/A"
        url = idata.get("url", "")
        is_fixable = "Yes" if issue.get("isUpgradable") or issue.get("isPatchable") else "No"

        lines.append(f"### [{severity.upper()}] {title}")
        lines.append(f"- Package: {pkg_name}@{pkg_ver}")
        lines.append(f"- CVE: {cve}")
        lines.append(f"- Fixable: {is_fixable}")
        if url:
            lines.append(f"- Details: {url}")
        lines.append("")

    if len(issues) > 50:
        lines.append(f"... and {len(issues) - 50} more issues (showing first 50)")

    return "\n".join(lines)


def handle_get_project_dependencies(params):
    org_id = _resolve_org_id(params.get("org"))
    project_id = params["project_id"]

    data = _snyk_request(
        f"{SNYK_V1_BASE}/org/{org_id}/project/{project_id}/dep-graph"
    )
    dep_graph = data.get("depGraph", {})
    pkgs = dep_graph.get("pkgs", [])

    lines = [f"Dependency graph for project {project_id}:\n"]
    lines.append(f"Total packages: {len(pkgs)}\n")

    root = dep_graph.get("graph", {}).get("rootNodeId", "")
    root_deps = []
    for node_id, node_data in dep_graph.get("graph", {}).get("nodes", [{}])[0:1]:
        pass  # graph structure varies

    # List all packages
    for pkg in pkgs[:100]:
        info = pkg.get("info", {})
        name = info.get("name", "?")
        version = info.get("version", "?")
        lines.append(f"- {name}@{version}")

    if len(pkgs) > 100:
        lines.append(f"\n... and {len(pkgs) - 100} more packages (showing first 100)")

    return "\n".join(lines)


def handle_search_issues_by_package(params):
    org_id = _resolve_org_id(params.get("org"))
    purl = params["purl"]

    # URL-encode the purl
    encoded_purl = urllib.request.quote(purl, safe="")

    data = _snyk_request(
        f"{SNYK_API_BASE}/rest/orgs/{org_id}/packages/{encoded_purl}/issues?version=2024-10-15",
        headers=_snyk_rest_headers(),
    )
    issues = data.get("data", [])

    lines = [f"Vulnerabilities for `{purl}`:\n"]
    lines.append(f"Found {len(issues)} known vulnerability(ies).\n")

    for issue in issues[:30]:
        attrs = issue.get("attributes", {})
        title = attrs.get("title", "Unknown")
        severity = attrs.get("effective_severity_level", "?")
        cves = [p.get("value") for p in attrs.get("problems", []) if p.get("source") == "CVE"]
        cve_str = ", ".join(cves) if cves else "N/A"

        lines.append(f"### [{severity.upper()}] {title}")
        lines.append(f"- CVE: {cve_str}")
        lines.append(f"- ID: {issue.get('id', 'N/A')}")
        lines.append("")

    return "\n".join(lines)


def handle_get_org_issues(params):
    org_id = _resolve_org_id(params.get("org"))
    limit = min(int(params.get("limit", 100)), 1000)

    query_params = [f"version=2024-10-15", f"limit={limit}"]
    if params.get("severity"):
        query_params.append(f"effective_severity_level={params['severity']}")
    if params.get("scan_type"):
        scan_type = "sast" if params["scan_type"] == "sast" else "package"
        query_params.append(f"scan_item.type={scan_type}")

    qs = "&".join(query_params)
    data = _snyk_request(
        f"{SNYK_API_BASE}/rest/orgs/{org_id}/issues?{qs}",
        headers=_snyk_rest_headers(),
    )
    issues = data.get("data", [])

    # Aggregate by severity
    counts = {}
    for issue in issues:
        sev = issue.get("attributes", {}).get("effective_severity_level", "unknown")
        counts[sev] = counts.get(sev, 0) + 1

    lines = [f"Organization issues summary ({len(issues)} returned):\n"]
    for sev in ["critical", "high", "medium", "low"]:
        if sev in counts:
            lines.append(f"- {sev.capitalize()}: {counts[sev]}")
    lines.append("")

    for issue in issues[:30]:
        attrs = issue.get("attributes", {})
        title = attrs.get("title", "Unknown")
        severity = attrs.get("effective_severity_level", "?")
        status = attrs.get("status", "?")
        lines.append(f"- [{severity.upper()}] {title} (status: {status})")

    if len(issues) > 30:
        lines.append(f"\n... and {len(issues) - 30} more (showing first 30)")

    return "\n".join(lines)


def handle_get_project_sbom(params):
    org_id = _resolve_org_id(params.get("org"))
    project_id = params["project_id"]
    fmt = params.get("format", "cyclonedx+json")
    version_param = "cyclonedx1.4+json" if "cyclonedx" in fmt else "spdx2.3+json"

    data = _snyk_request(
        f"{SNYK_API_BASE}/rest/orgs/{org_id}/projects/{project_id}/sbom?version=2024-10-15&format={version_param}",
        headers=_snyk_rest_headers(),
    )

    # Return a summary rather than the full SBOM (can be huge)
    if "cyclonedx" in fmt:
        components = data.get("components", [])
        lines = [f"SBOM (CycloneDX) for project {project_id}:\n"]
        lines.append(f"Total components: {len(components)}\n")
        for comp in components[:50]:
            lines.append(f"- {comp.get('name', '?')}@{comp.get('version', '?')} ({comp.get('type', '?')})")
        if len(components) > 50:
            lines.append(f"\n... and {len(components) - 50} more")
    else:
        packages = data.get("packages", [])
        lines = [f"SBOM (SPDX) for project {project_id}:\n"]
        lines.append(f"Total packages: {len(packages)}\n")
        for pkg in packages[:50]:
            lines.append(f"- {pkg.get('name', '?')}@{pkg.get('versionInfo', '?')}")
        if len(packages) > 50:
            lines.append(f"\n... and {len(packages) - 50} more")

    return "\n".join(lines)


TOOL_HANDLERS = {
    "list_organizations": handle_list_organizations,
    "list_projects": handle_list_projects,
    "get_project_issues": handle_get_project_issues,
    "get_project_dependencies": handle_get_project_dependencies,
    "search_issues_by_package": handle_search_issues_by_package,
    "get_org_issues": handle_get_org_issues,
    "get_project_sbom": handle_get_project_sbom,
}


# ============================================================================
# JSON-RPC Helpers
# ============================================================================


def jsonrpc_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


# ============================================================================
# HTTP Handler
# ============================================================================


class SnykMcpHandler(BaseHTTPRequestHandler):
    """HTTP handler for MCP Streamable HTTP transport."""

    def log_message(self, format, *args):
        logger.info(format % args)

    def _send_json(self, status_code, body, session_id=None):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def do_GET(self):
        """Health check."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "healthy", "service": "snyk-mcp"}).encode())

    def do_POST(self):
        """Handle MCP JSON-RPC requests."""
        if self.path not in (MCP_PATH, MCP_PATH + "/"):
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, jsonrpc_error(None, -32600, "Empty request body"))
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, jsonrpc_error(None, -32600, "Invalid JSON"))
            return

        request_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        if method == "initialize":
            session_id = os.urandom(16).hex()
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Snyk MCP Server (API)", "version": "1.0.0"},
            }
            self._send_json(200, jsonrpc_response(request_id, result), session_id=session_id)
            return

        if method == "notifications/initialized":
            self.send_response(204)
            self.end_headers()
            return

        if method == "tools/list":
            result = {"tools": MCP_TOOLS}
            self._send_json(200, jsonrpc_response(request_id, result))
            return

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})

            handler = TOOL_HANDLERS.get(tool_name)
            if not handler:
                self._send_json(
                    200, jsonrpc_error(request_id, -32602, f"Unknown tool: {tool_name}")
                )
                return

            try:
                text_result = handler(tool_args)
                result = {"content": [{"type": "text", "text": text_result}]}
                self._send_json(200, jsonrpc_response(request_id, result))
            except Exception as e:
                logger.error(f"Tool {tool_name} error: {e}", exc_info=True)
                result = {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                }
                self._send_json(200, jsonrpc_response(request_id, result))
            return

        self._send_json(200, jsonrpc_error(request_id, -32601, f"Method not found: {method}"))


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    if not SNYK_TOKEN:
        logger.error("SNYK_TOKEN environment variable is required")
        exit(1)

    logger.info(f"Starting Snyk MCP Server on port {PORT}")
    logger.info(f"MCP endpoint: http://0.0.0.0:{PORT}{MCP_PATH}")
    logger.info(f"Snyk org: {SNYK_ORG_ID or '(auto-detect)'}")

    server = HTTPServer(("0.0.0.0", PORT), SnykMcpHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.server_close()
