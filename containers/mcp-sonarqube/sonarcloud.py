"""Native SonarQube Cloud tools for the OutcomeOps MCP server.

Replaces the third-party SonarQube MCP Fargate service, whose async HttpClient
I/O reactor shuts down under concurrent load (IOReactorShutdownException) and
never recovers. These call the SonarCloud Web REST API directly, one fresh
connection per request (urllib) -- no shared async reactor, no connection pool
to leak, no concurrency fault. Structurally immune to that class of bug.

Config (env, wired from the MCP server task):
  SONARQUBE_URL            base URL (default https://sonarcloud.io)
  SONARQUBE_ORG            organization key (e.g. "bcarpio")
  SONARQUBE_TOKEN_SSM_PATH SSM SecureString path holding the API token

Tools keep the exact names + parameter names of the tools they replace so
nothing downstream (chat prompts, agents) has to change.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_TOKEN_CACHE = {"value": None}


def _base_url():
    return os.environ.get("SONARQUBE_URL", "https://sonarcloud.io").rstrip("/")


def _org():
    return os.environ.get("SONARQUBE_ORG", "")


def _token():
    if _TOKEN_CACHE["value"]:
        return _TOKEN_CACHE["value"]
    # Prefer the ECS-injected secret env (SONARQUBE_TOKEN from SSM, same pattern
    # as the other MCP containers). Fall back to reading SSM directly if only a
    # path is provided.
    tok = os.environ.get("SONARQUBE_TOKEN")
    if tok:
        _TOKEN_CACHE["value"] = tok
        return tok
    path = os.environ.get("SONARQUBE_TOKEN_SSM_PATH", "")
    if not path:
        return None
    import boto3  # lazy

    val = boto3.client("ssm").get_parameter(
        Name=path, WithDecryption=True
    )["Parameter"]["Value"]
    _TOKEN_CACHE["value"] = val
    return val


def _get(api_path, params):
    """Authenticated GET to the SonarCloud Web API. Fresh connection per call.

    Returns (json_or_none, error_or_none).
    """
    token = _token()
    if not token:
        return None, "SonarQube token not configured (SONARQUBE_TOKEN_SSM_PATH)"
    # drop empty params
    query = {k: v for k, v in params.items() if v not in (None, "", [])}
    url = f"{_base_url()}{api_path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return None, f"SonarCloud API {e.code}: {body}"
    except urllib.error.URLError as e:
        return None, f"SonarCloud API unreachable: {e.reason}"
    except (ValueError, TimeoutError) as e:
        return None, f"SonarCloud API error: {e}"


def _csv(value):
    """Join a list arg to CSV; pass a string through. This is where the upstream
    server's 'String cannot be cast to List' bug lived -- we format it here."""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return value


def _ok(payload):
    return {"text": json.dumps(payload)}


def _err(msg):
    return {"error": msg}


# --- tools (names/params mirror the upstream tools they replace) -----------

def search_my_sonarqube_projects(args, _email=None):
    body, err = _get("/api/components/search_projects", {
        "organization": _org(),
        "p": args.get("page"),
        "ps": args.get("pageSize", 100),
        "filter": f'query="{args["q"]}"' if args.get("q") else None,
    })
    if err:
        return _err(err)
    projects = [{"key": c.get("key"), "name": c.get("name")}
                for c in body.get("components", [])]
    return _ok({"projects": projects, "paging": body.get("paging", {})})


def get_project_quality_gate_status(args, _email=None):
    body, err = _get("/api/qualitygates/project_status", {
        "projectKey": args.get("projectKey"),
        "analysisId": args.get("analysisId"),
        "projectId": args.get("projectId"),
        "pullRequest": args.get("pullRequest"),
    })
    if err:
        return _err(err)
    # unwrap projectStatus to match the upstream tool's output shape
    return _ok(body.get("projectStatus", body))


def get_component_measures(args, _email=None):
    body, err = _get("/api/measures/component", {
        "component": args.get("projectKey") or args.get("component"),
        "metricKeys": _csv(args.get("metricKeys")),
        "pullRequest": args.get("pullRequest"),
    })
    if err:
        return _err(err)
    return _ok(body)


def search_sonar_issues_in_projects(args, _email=None):
    body, err = _get("/api/issues/search", {
        "organization": _org(),
        "projects": _csv(args.get("projects")),
        "components": _csv(args.get("files")),
        "issues": _csv(args.get("issueKey")),
        # The schema advertises the new impact-severity vocab (INFO/LOW/MEDIUM/
        # HIGH/BLOCKER), which SonarCloud takes via impactSeverities -- NOT the
        # legacy `severities` param (INFO/MINOR/MAJOR/CRITICAL/BLOCKER).
        "impactSeverities": _csv(args.get("severities")),
        "issueStatuses": _csv(args.get("issueStatuses")),
        "impactSoftwareQualities": _csv(args.get("impactSoftwareQualities")),
        "pullRequest": args.get("pullRequestId"),
        "p": args.get("p"),
        "ps": args.get("ps", 100),
    })
    if err:
        return _err(err)
    return _ok(body)


def search_security_hotspots(args, _email=None):
    body, err = _get("/api/hotspots/search", {
        "organization": _org(),
        "projectKey": args.get("projectKey"),
        "status": args.get("status"),
        "resolution": args.get("resolution"),
        "pullRequest": args.get("pullRequest"),
        "p": args.get("p"),
        "ps": args.get("ps", 100),
    })
    if err:
        return _err(err)
    return _ok(body)


# --- MCP tool registry (bare names; the gateway/chat catalog namespaces them
#     as sonarqube__<name> via the server_name prefix) -----------------------

TOOL_DEFS = [
    {
        "name": "search_my_sonarqube_projects",
        "description": "[SonarQube] Find SonarQube Cloud projects in your organization. Use this first when the project key is unknown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Optional text to filter projects by name/key."},
                "pageSize": {"type": "number", "description": "Page size (default 100, max 500)."},
                "page": {"type": "string", "description": "Optional page number (default 1)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_project_quality_gate_status",
        "description": "[SonarQube] Get the Quality Gate status for a project (pass/fail + failing conditions).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectKey": {"type": "string", "description": "The project key."},
                "analysisId": {"type": "string"},
                "projectId": {"type": "string"},
                "pullRequest": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_component_measures",
        "description": "[SonarQube] Get measures for a project (bugs, vulnerabilities, code_smells, coverage, ncloc, ratings, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectKey": {"type": "string", "description": "The project key."},
                "metricKeys": {"type": "array", "items": {"type": "string"}, "description": "Metric keys, e.g. ['bugs','coverage','ncloc']."},
                "pullRequest": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "search_sonar_issues_in_projects",
        "description": "[SonarQube] Search issues (bugs, vulnerabilities, code smells). Filter by severities=['HIGH','BLOCKER'], impactSoftwareQualities=['SECURITY'], issueStatuses=['OPEN'].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projects": {"type": "array", "items": {"type": "string"}, "description": "Project keys to search."},
                "severities": {"type": "array", "items": {"type": "string", "enum": ["INFO", "LOW", "MEDIUM", "HIGH", "BLOCKER"]}},
                "issueStatuses": {"type": "array", "items": {"type": "string", "enum": ["OPEN", "CONFIRMED", "FALSE_POSITIVE", "ACCEPTED", "FIXED"]}},
                "impactSoftwareQualities": {"type": "array", "items": {"type": "string", "enum": ["MAINTAINABILITY", "RELIABILITY", "SECURITY"]}},
                "files": {"type": "array", "items": {"type": "string"}},
                "issueKey": {"type": "array", "items": {"type": "string"}},
                "pullRequestId": {"type": "string"},
                "p": {"type": "number", "description": "Page number."},
                "ps": {"type": "number", "description": "Page size (default 100, max 500)."},
            },
            "required": [],
        },
    },
    {
        "name": "search_security_hotspots",
        "description": "[SonarQube] Search security hotspots for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "projectKey": {"type": "string", "description": "The project key."},
                "status": {"type": "string", "description": "TO_REVIEW or REVIEWED."},
                "resolution": {"type": "string"},
                "pullRequest": {"type": "string"},
                "p": {"type": "number"},
                "ps": {"type": "number"},
            },
            "required": [],
        },
    },
]

TOOL_MAP = {
    "search_my_sonarqube_projects": search_my_sonarqube_projects,
    "get_project_quality_gate_status": get_project_quality_gate_status,
    "get_component_measures": get_component_measures,
    "search_sonar_issues_in_projects": search_sonar_issues_in_projects,
    "search_security_hotspots": search_security_hotspots,
}
