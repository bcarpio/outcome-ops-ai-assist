/**
 * Streaming proxy backend for OutcomeOps Chat UI
 *
 * This server:
 * 1. Serves static React build files
 * 2. Proxies /api/chat to Lambda Function URL with IAM auth (SigV4)
 * 3. Streams the response back to the browser
 *
 * Security: Lambda Function URL requires IAM auth.
 * The Fargate task role has permission to invoke the Function URL.
 * Requests are signed with SigV4 using task credentials.
 */

import express from "express";
import cookieParser from "cookie-parser";
import jwt from "jsonwebtoken";
import { SignatureV4 } from "@aws-sdk/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";
import { readFileSync, existsSync } from "fs";
import { join, extname } from "path";

const app = express();
// Trust the ALB in front of us so req.ip is the original client IP from
// X-Forwarded-For rather than the ALB's IP. ALB always replaces XFF on
// inbound requests, so trusting it here is safe behind ALB and lets us
// forward the real browser IP to downstream Lambdas for audit logging.
app.set("trust proxy", true);
const PORT = process.env.PORT || 3000;
const LAMBDA_FUNCTION_URL = process.env.LAMBDA_FUNCTION_URL;
const WORKSPACE_LAMBDA_URL = process.env.WORKSPACE_LAMBDA_URL;
const CONFLUENCE_LAMBDA_URL = process.env.CONFLUENCE_LAMBDA_URL;
const JIRA_LAMBDA_URL = process.env.JIRA_LAMBDA_URL;
const GITHUB_LAMBDA_URL = process.env.GITHUB_LAMBDA_URL;
const OUTLOOK_LAMBDA_URL = process.env.OUTLOOK_LAMBDA_URL;
const TEAMS_LAMBDA_URL = process.env.TEAMS_LAMBDA_URL;
const SHAREPOINT_LAMBDA_URL = process.env.SHAREPOINT_LAMBDA_URL;
const AWS_REGION = process.env.AWS_REGION || "us-west-2";
const STATIC_DIR = process.env.STATIC_DIR || "/app/static";
const LOCAL_DEV_MODE = process.env.LOCAL_DEV_MODE === "true";
const SCANNER_JWT_SECRET_SSM = process.env.SCANNER_JWT_SECRET_SSM || "";

// Scanner JWT secret loaded from SSM at startup (null = scanner auth disabled)
let scannerJwtSecret = null;

async function loadScannerSecret() {
  if (!SCANNER_JWT_SECRET_SSM) return;
  try {
    const ssm = new SSMClient({});
    const result = await ssm.send(
      new GetParameterCommand({ Name: SCANNER_JWT_SECRET_SSM, WithDecryption: true })
    );
    scannerJwtSecret = result.Parameter.Value;
    console.log("[scanner] JWT secret loaded");
  } catch (err) {
    console.log("[scanner] No scanner JWT secret configured, scanner auth disabled");
  }
}

// MIME types for static file serving
const MIME_TYPES = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
};

// Initialize SigV4 signer
const signer = new SignatureV4({
  service: "lambda",
  region: AWS_REGION,
  credentials: defaultProvider(),
  sha256: Sha256,
});

// Parse JSON bodies and cookies
app.use(express.json({ limit: "1mb" }));
app.use(cookieParser());

// Require authentication and JSON content type for all API routes
app.use("/api", (req, res, next) => {
  const user = extractUser(req);
  if (user.email === "anonymous") {
    return res.status(401).json({ error: "Authentication required" });
  }
  // Reject non-JSON content types on requests with bodies
  const ct = req.headers["content-type"] || "";
  if (req.method !== "GET" && req.method !== "DELETE" && ct && !ct.includes("application/json")) {
    return res.status(415).json({ error: "Unsupported Media Type. Use application/json" });
  }
  req.user = user;
  next();
});

// Health check endpoint
app.get("/health", (req, res) => {
  res.json({ status: "healthy" });
});

/**
 * Extract user identity from ALB OIDC headers.
 * When OIDC is enabled, ALB sets:
 *   - x-amzn-oidc-data: JWT with claims from UserInfo endpoint
 *   - x-amzn-oidc-accesstoken: Raw Azure AD access token
 *   - x-amzn-oidc-identity: The sub claim
 *
 * Azure AD's UserInfo endpoint may not return 'email' for cloud-only
 * accounts (@onmicrosoft.com). In that case, we decode the access token
 * to get 'upn' (User Principal Name) as fallback.
 */
function extractUser(req) {
  // Path 1: OIDC headers from ALB (primary auth)
  const oidcData = req.headers["x-amzn-oidc-data"];
  if (oidcData) {
    try {
      // ALB OIDC JWT is base64url-encoded; payload is the second segment
      const payload = JSON.parse(
        Buffer.from(oidcData.split(".")[1], "base64url").toString()
      );

      let email = payload.email || payload.preferred_username || null;

      // Azure AD cloud-only accounts may not have email in UserInfo.
      // Fall back to decoding the access token for upn/preferred_username.
      if (!email) {
        const accessToken = req.headers["x-amzn-oidc-accesstoken"];
        if (accessToken) {
          try {
            const tokenPayload = JSON.parse(
              Buffer.from(accessToken.split(".")[1], "base64url").toString()
            );
            email = tokenPayload.upn || tokenPayload.preferred_username || tokenPayload.unique_name || null;
            if (email) {
              console.log(`[auth] Resolved email from access token: ${email}`);
            }
          } catch {
            // Access token may not be a decodable JWT (opaque tokens)
          }
        }
      }

      return {
        email: (email || payload.sub || "unknown").toLowerCase(),
        name: payload.name || email || "unknown",
      };
    } catch {
      return { email: "unknown", name: "unknown" };
    }
  }

  // Path 2: Scanner session cookie (for AWS Security Agent)
  const scannerToken = req.cookies?.oo_scanner_session;
  if (scannerToken && scannerJwtSecret) {
    try {
      const decoded = jwt.verify(scannerToken, scannerJwtSecret);
      if (decoded.auth_type === "scanner") {
        return {
          email: decoded.email,
          name: decoded.name || "Security Scanner",
        };
      }
    } catch {
      // Invalid or expired token -- fall through to anonymous
    }
  }

  return { email: "anonymous", name: "anonymous" };
}

/**
 * Build the standard set of headers we forward to every signed Lambda
 * call: the user's email plus the browser's real IP / user-agent (the
 * latter two are read by the Lambda's main.py shim and surface into
 * audit rows so we capture *who*, *what*, and *from where* on every
 * chat / RAG / admin call). All chat & workspace-mgmt proxies use this.
 */
function userHeaders(req, user) {
  return {
    "x-user-email": user.email,
    "x-real-source-ip": req.ip || "",
    "x-real-user-agent": req.get("user-agent") || "",
  };
}

/**
 * Sign and send a request to a Lambda Function URL.
 * Returns the fetch Response object.
 */
async function signedFetch(lambdaUrl, method, path, { body, extraHeaders } = {}) {
  const url = new URL(lambdaUrl);

  // Parse path and query string separately for proper SigV4 signing
  const [pathPart, queryString] = path.split("?");
  const targetPath = url.pathname.replace(/\/$/, "") + pathPart;

  // Parse query string into object for SigV4 - signer expects decoded values
  const query = {};
  if (queryString) {
    const params = new URLSearchParams(queryString);
    for (const [key, value] of params) {
      query[key] = value;
    }
  }

  const request = {
    method,
    protocol: url.protocol,
    hostname: url.hostname,
    path: targetPath,
    headers: {
      "Content-Type": "application/json",
      host: url.hostname,
      ...extraHeaders,
    },
  };
  if (Object.keys(query).length > 0) request.query = query;
  if (body) request.body = body;

  const signedRequest = await signer.sign(request);

  // Rebuild query string from parsed values to ensure consistency with signing
  // The signer encodes the values, so we must use the same encoding for the actual request
  const rebuiltQueryString = Object.keys(query).length > 0
    ? Object.entries(query)
        .sort(([a], [b]) => a.localeCompare(b)) // SigV4 requires sorted params
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
        .join("&")
    : "";

  const fullUrl = rebuiltQueryString
    ? `${url.origin}${targetPath}?${rebuiltQueryString}`
    : `${url.origin}${targetPath}`;

  return fetch(fullUrl, {
    method,
    headers: signedRequest.headers,
    body: body || undefined,
    redirect: "manual",
  });
}

/**
 * Sign and send a request to the Chat Lambda Function URL.
 */
async function signedChatFetch(method, path, options = {}) {
  return signedFetch(LAMBDA_FUNCTION_URL, method, path, options);
}

/**
 * Sign and send a request to the Workspace Lambda Function URL.
 */
async function signedWorkspaceFetch(method, path, options = {}) {
  return signedFetch(WORKSPACE_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the Confluence Lambda Function URL.
 */
async function signedConfluenceFetch(method, path, options = {}) {
  return signedFetch(CONFLUENCE_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the Jira Lambda Function URL.
 */
async function signedJiraFetch(method, path, options = {}) {
  return signedFetch(JIRA_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the GitHub Lambda Function URL.
 */
async function signedGitHubFetch(method, path, options = {}) {
  return signedFetch(GITHUB_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the Outlook Lambda Function URL.
 */
async function signedOutlookFetch(method, path, options = {}) {
  return signedFetch(OUTLOOK_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the Teams Lambda Function URL.
 */
async function signedTeamsFetch(method, path, options = {}) {
  return signedFetch(TEAMS_LAMBDA_URL, method, path, options);
}

/**
 * Sign and send a request to the SharePoint Lambda Function URL.
 */
async function signedSharePointFetch(method, path, options = {}) {
  return signedFetch(SHAREPOINT_LAMBDA_URL, method, path, options);
}

// Streaming chat proxy endpoint with SigV4 auth
app.post("/api/chat", async (req, res) => {
  if (!LAMBDA_FUNCTION_URL) {
    console.error("[proxy] LAMBDA_FUNCTION_URL not configured");
    res.status(500).json({ error: "Lambda function URL not configured" });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] User: ${user.email}`);
  console.log(`[proxy] Chat request: ${JSON.stringify(req.body).slice(0, 200)}...`);

  try {
    const body = JSON.stringify({ ...req.body, user });

    const response = await signedChatFetch("POST", "/", {
      body,
      extraHeaders: userHeaders(req, user),
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText });
      return;
    }

    // Set headers for NDJSON streaming
    res.setHeader("Content-Type", "application/x-ndjson");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");

    // Stream the response
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      res.write(chunk);
    }

    res.end();
    console.log("[proxy] Stream completed");
  } catch (error) {
    console.error(`[proxy] Error: ${error.message}`);

    // If headers not sent, send error response
    if (!res.headersSent) {
      res.status(500).json({ error: "Failed to process request" });
    } else {
      // If streaming already started, send error as NDJSON
      res.write(JSON.stringify({ type: "error", error: error.message }) + "\n");
      res.end();
    }
  }
});

// ============================================================================
// REST Conversation API (proxied to Lambda with SigV4)
// ============================================================================

// Share/unshare a conversation (PUT with workspace_ids; empty list = unshare)
app.put("/api/conversations/:id/share", async (req, res) => {
  try {
    const user = extractUser(req);
    const response = await signedChatFetch("PUT", `/conversations/${req.params.id}/share`, {
      body: JSON.stringify(req.body),
      extraHeaders: userHeaders(req, user),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    console.error("Share conversation error:", err.message);
    res.status(500).json({ error: "Failed to share conversation" });
  }
});

// List conversations for the authenticated user
app.get("/api/conversations", async (req, res) => {
  if (!LAMBDA_FUNCTION_URL) {
    res.status(500).json({ error: "Lambda function URL not configured" });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] List conversations for: ${user.email}`);

  try {
    // Forward pagination query params to Lambda
    const params = new URLSearchParams();
    if (req.query.limit) params.set("limit", req.query.limit);
    if (req.query.cursor) params.set("cursor", req.query.cursor);
    const qs = params.toString();
    const path = qs ? `/conversations?${qs}` : "/conversations";

    const response = await signedChatFetch("GET", path, {
      extraHeaders: userHeaders(req, user),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    console.error(`[proxy] Error listing conversations: ${error.message}`);
    res.status(500).json({ error: "Failed to list conversations" });
  }
});

// Get conversation with messages
app.get("/api/conversations/:id", async (req, res) => {
  if (!LAMBDA_FUNCTION_URL) {
    res.status(500).json({ error: "Lambda function URL not configured" });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Get conversation ${req.params.id} for: ${user.email}`);

  try {
    const response = await signedChatFetch("GET", `/conversations/${req.params.id}`, {
      extraHeaders: userHeaders(req, user),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    console.error(`[proxy] Error getting conversation: ${error.message}`);
    res.status(500).json({ error: "Failed to get conversation" });
  }
});

// Delete conversation
app.delete("/api/conversations/:id", async (req, res) => {
  if (!LAMBDA_FUNCTION_URL) {
    res.status(500).json({ error: "Lambda function URL not configured" });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Delete conversation ${req.params.id} for: ${user.email}`);

  try {
    const response = await signedChatFetch("DELETE", `/conversations/${req.params.id}`, {
      extraHeaders: userHeaders(req, user),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    console.error(`[proxy] Error deleting conversation: ${error.message}`);
    res.status(500).json({ error: "Failed to delete conversation" });
  }
});

// ============================================================================
// Shared Conversations API (proxied to Lambda with SigV4)
// ============================================================================

// List shared conversations
app.get("/api/shared", async (req, res) => {
  try {
    const user = extractUser(req);
    const params = new URLSearchParams();
    if (req.query.workspace_id) params.set("workspace_id", req.query.workspace_id);
    if (req.query.limit) params.set("limit", req.query.limit);
    if (req.query.cursor) params.set("cursor", req.query.cursor);
    const path = `/shared?${params.toString()}`;
    const response = await signedChatFetch("GET", path, {
      extraHeaders: userHeaders(req, user),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (err) {
    console.error("List shared conversations error:", err.message);
    res.status(500).json({ error: "Failed to list shared conversations" });
  }
});

// ============================================================================
// Workspace Management API (proxied to Workspace Lambda with SigV4)
// ============================================================================

// In-memory mock data for local development
const mockWorkspaces = new Map();
const mockMembers = new Map();
const mockRepos = new Map();

// Mock workspace handlers for local development
function handleMockWorkspaceRequest(req, res, method, path) {
  const user = extractUser(req);
  const userEmail = user.email || "local@dev.com";
  console.log(`[mock] Workspace ${method} ${path} for: ${userEmail}`);

  const pathParts = path.split("/").filter(Boolean);

  // POST /workspaces - Create workspace
  if (method === "POST" && pathParts.length === 1 && pathParts[0] === "workspaces") {
    const { name, description } = req.body;
    const id = `ws-${Date.now()}`;
    const workspace = {
      workspace_id: id,
      name,
      description: description || "",
      created_at: new Date().toISOString(),
      created_by: userEmail,
    };
    mockWorkspaces.set(id, workspace);
    mockMembers.set(id, [{ user_email: userEmail, role: "workspace_admin", joined_at: new Date().toISOString() }]);
    mockRepos.set(id, []);
    return res.status(201).json({ workspace });
  }

  // GET /workspaces - List workspaces
  if (method === "GET" && pathParts.length === 1 && pathParts[0] === "workspaces") {
    const workspaces = Array.from(mockWorkspaces.values()).map(ws => ({
      ...ws,
      role: "workspace_admin",
    }));
    return res.json({ workspaces });
  }

  // GET /workspaces/:id - Get workspace
  if (method === "GET" && pathParts.length === 2 && pathParts[0] === "workspaces") {
    const workspace = mockWorkspaces.get(pathParts[1]);
    if (!workspace) return res.status(404).json({ error: "Workspace not found" });
    return res.json({ workspace: { ...workspace, role: "workspace_admin" } });
  }

  // DELETE /workspaces/:id - Delete workspace
  if (method === "DELETE" && pathParts.length === 2 && pathParts[0] === "workspaces") {
    mockWorkspaces.delete(pathParts[1]);
    mockMembers.delete(pathParts[1]);
    mockRepos.delete(pathParts[1]);
    return res.json({ deleted: pathParts[1] });
  }

  // GET /workspaces/:id/members - List members
  if (method === "GET" && pathParts.length === 3 && pathParts[2] === "members") {
    const members = mockMembers.get(pathParts[1]) || [];
    return res.json({ members });
  }

  // POST /workspaces/:id/members - Add member
  if (method === "POST" && pathParts.length === 3 && pathParts[2] === "members") {
    const { email, role } = req.body;
    const members = mockMembers.get(pathParts[1]) || [];
    const member = { user_email: email, role: role || "member", joined_at: new Date().toISOString() };
    members.push(member);
    mockMembers.set(pathParts[1], members);
    return res.status(201).json({ member });
  }

  // GET /workspaces/:id/repos - List repos
  if (method === "GET" && pathParts.length === 3 && pathParts[2] === "repos") {
    const repos = mockRepos.get(pathParts[1]) || [];
    return res.json({ repos });
  }

  // GET /workspaces/:id/repos/available - List available repos (mock)
  if (method === "GET" && pathParts.length === 4 && pathParts[2] === "repos" && pathParts[3] === "available") {
    return res.json({
      repos: [
        { full_name: "org/repo-1", name: "repo-1", description: "Mock repo 1", private: false },
        { full_name: "org/repo-2", name: "repo-2", description: "Mock repo 2", private: true },
        { full_name: "org/repo-3", name: "repo-3", description: "Mock repo 3", private: false },
      ],
    });
  }

  // POST /workspaces/:id/repos - Add repo
  if (method === "POST" && pathParts.length === 3 && pathParts[2] === "repos") {
    const { repo_name } = req.body;
    const repos = mockRepos.get(pathParts[1]) || [];
    const repo = { repo_name, enabled: true, added_at: new Date().toISOString(), added_by: userEmail };
    repos.push(repo);
    mockRepos.set(pathParts[1], repos);
    return res.status(201).json({ repo });
  }

  // DELETE /workspaces/:id/repos/:name - Remove repo
  if (method === "DELETE" && pathParts.length === 4 && pathParts[2] === "repos") {
    const repoName = decodeURIComponent(pathParts[3]);
    const repos = mockRepos.get(pathParts[1]) || [];
    const filtered = repos.filter(r => r.repo_name !== repoName);
    mockRepos.set(pathParts[1], filtered);
    return res.json({ removed: repoName });
  }

  return res.status(404).json({ error: "Not found" });
}

// Helper to proxy workspace requests
async function proxyWorkspaceRequest(req, res, method, path) {
  // Use mock handlers in local development mode
  if (LOCAL_DEV_MODE) {
    return handleMockWorkspaceRequest(req, res, method, path);
  }

  if (!WORKSPACE_LAMBDA_URL) {
    console.log("[proxy] WORKSPACE_LAMBDA_URL not configured - workspaces feature disabled");
    res.status(404).json({ error: "Workspaces feature not enabled. Set WORKSPACE_LAMBDA_URL or LOCAL_DEV_MODE=true." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Workspace ${method} ${path} for: ${user.email}`);

  try {
    const options = { extraHeaders: userHeaders(req, user) };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedWorkspaceFetch(method, path, options);

    // Handle non-OK responses
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Workspace Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "Workspace request failed" });
      return;
    }

    // Parse JSON response safely
    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from workspace Lambda");
      res.status(500).json({ error: "Empty response from workspace service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse workspace response: ${text}`);
      res.status(500).json({ error: "Invalid response from workspace service" });
    }
  } catch (error) {
    console.error(`[proxy] Workspace error: ${error.message}`);
    // Provide more helpful error for credential issues
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured. Run locally with AWS credentials or deploy to Fargate." });
    } else {
      res.status(500).json({ error: `Failed to process workspace request: ${error.message}` });
    }
  }
}

// Workspace CRUD
app.post("/api/workspaces", (req, res) => proxyWorkspaceRequest(req, res, "POST", "/workspaces"));
app.get("/api/workspaces", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/workspaces"));
app.get("/api/workspaces/:id", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}`));
app.patch("/api/workspaces/:id", (req, res) => proxyWorkspaceRequest(req, res, "PATCH", `/workspaces/${req.params.id}`));
app.delete("/api/workspaces/:id", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}`));

// Workspace members
app.post("/api/workspaces/:id/members", (req, res) => proxyWorkspaceRequest(req, res, "POST", `/workspaces/${req.params.id}/members`));
app.get("/api/workspaces/:id/members", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/members`));
app.patch("/api/workspaces/:id/members/:email", (req, res) => proxyWorkspaceRequest(req, res, "PATCH", `/workspaces/${req.params.id}/members/${encodeURIComponent(req.params.email)}`));
app.delete("/api/workspaces/:id/members/:email", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/members/${encodeURIComponent(req.params.email)}`));

// Workspace repos
app.get("/api/workspaces/:id/repos/available", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/repos/available`));
app.get("/api/workspaces/:id/repos", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/repos`));
app.post("/api/workspaces/:id/repos", (req, res) => proxyWorkspaceRequest(req, res, "POST", `/workspaces/${req.params.id}/repos`));
app.delete("/api/workspaces/:id/repos/*", (req, res) => {
  // Extract repo name from the wildcard path (supports slashes like "owner/repo")
  const repoName = req.params[0];
  proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/repos/${encodeURIComponent(repoName)}`);
});

// Workspace sharing
app.get("/api/workspaces/:id/sharing", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/sharing`));
app.put("/api/workspaces/:id/sharing", (req, res) => proxyWorkspaceRequest(req, res, "PUT", `/workspaces/${req.params.id}/sharing`));
app.get("/api/workspaces/:id/sharing/available", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/sharing/available`));

// Workspace system prompt
app.get("/api/workspaces/:id/system-prompt", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/system-prompt`));
app.get("/api/workspaces/:id/budget", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/budget`));
app.put("/api/workspaces/:id/budget", (req, res) => proxyWorkspaceRequest(req, res, "PUT", `/workspaces/${req.params.id}/budget`));
app.put("/api/workspaces/:id/system-prompt", (req, res) => proxyWorkspaceRequest(req, res, "PUT", `/workspaces/${req.params.id}/system-prompt`));
app.delete("/api/workspaces/:id/system-prompt", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/system-prompt`));

// Workspace documents
app.post("/api/workspaces/:id/documents/upload", (req, res) => proxyWorkspaceRequest(req, res, "POST", `/workspaces/${req.params.id}/documents/upload`));
app.get("/api/workspaces/:id/documents", (req, res) => {
  // Pass through query params
  const prefix = req.query.prefix ? `?prefix=${encodeURIComponent(req.query.prefix)}` : "";
  proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/documents${prefix}`);
});
app.delete("/api/workspaces/:id/documents/*", (req, res) => {
  // Extract document path from the wildcard (supports slashes in path)
  const documentPath = req.params[0];
  proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/documents/${encodeURIComponent(documentPath)}`);
});

// ============================================================================
// Org Management API (proxied to Workspace Lambda with SigV4)
// ============================================================================

// User search
app.get("/api/users/search", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/users/search?q=${encodeURIComponent(req.query.q || "")}`));

// Org members
app.get("/api/org/members", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/org/members"));
app.post("/api/org/members", (req, res) => proxyWorkspaceRequest(req, res, "POST", "/org/members"));
app.patch("/api/org/members/:email", (req, res) => proxyWorkspaceRequest(req, res, "PATCH", `/org/members/${encodeURIComponent(req.params.email)}`));
app.delete("/api/org/members/:email", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/org/members/${encodeURIComponent(req.params.email)}`));

// Chat searchable workspaces
app.get("/api/chat/searchable-workspaces", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/chat/searchable-workspaces"));

// Chat available prompts
app.get("/api/chat/available-prompts", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/chat/available-prompts"));

// ============================================================================
// MCP Server Management (org admin)
// ============================================================================
app.post("/api/mcp/servers", (req, res) => proxyWorkspaceRequest(req, res, "POST", "/mcp/servers"));
app.get("/api/mcp/servers", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/mcp/servers"));
app.get("/api/mcp/servers/:id", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/mcp/servers/${req.params.id}`));
app.patch("/api/mcp/servers/:id", (req, res) => proxyWorkspaceRequest(req, res, "PATCH", `/mcp/servers/${req.params.id}`));
app.delete("/api/mcp/servers/:id", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/mcp/servers/${req.params.id}`));
app.post("/api/mcp/servers/:id/refresh", (req, res) => proxyWorkspaceRequest(req, res, "POST", `/mcp/servers/${req.params.id}/refresh`));
app.get("/api/mcp/servers/:id/workspaces", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/mcp/servers/${req.params.id}/workspaces`));

// MCP workspace assignments
app.post("/api/workspaces/:id/mcp-servers", (req, res) => proxyWorkspaceRequest(req, res, "POST", `/workspaces/${req.params.id}/mcp-servers`));
app.get("/api/workspaces/:id/mcp-servers", (req, res) => proxyWorkspaceRequest(req, res, "GET", `/workspaces/${req.params.id}/mcp-servers`));
app.delete("/api/workspaces/:id/mcp-servers/:mcpId", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/mcp-servers/${req.params.mcpId}`));

// Chat MCP servers
app.get("/api/chat/mcp-servers", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/chat/mcp-servers"));

// API Key management
app.post("/api/api-keys", (req, res) => proxyWorkspaceRequest(req, res, "POST", "/api-keys"));
app.get("/api/api-keys", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/api-keys"));
app.delete("/api/api-keys/:keyId", (req, res) => proxyWorkspaceRequest(req, res, "DELETE", `/api-keys/${req.params.keyId}`));

// ============================================================================
// Analytics API (proxied to Workspace Lambda)
// ============================================================================
app.get("/api/analytics/budgets", (req, res) => proxyWorkspaceRequest(req, res, "GET", "/analytics/budgets"));
app.get("/api/analytics/summary", (req, res) => {
  const params = new URLSearchParams();
  if (req.query.start_date) params.set("start_date", req.query.start_date);
  if (req.query.end_date) params.set("end_date", req.query.end_date);
  if (req.query.workspace_id) params.set("workspace_id", req.query.workspace_id);
  const qs = params.toString();
  const path = qs ? `/analytics/summary?${qs}` : "/analytics/summary";
  proxyWorkspaceRequest(req, res, "GET", path);
});

app.get("/api/analytics/users", (req, res) => {
  const params = new URLSearchParams();
  if (req.query.start_date) params.set("start_date", req.query.start_date);
  if (req.query.end_date) params.set("end_date", req.query.end_date);
  if (req.query.workspace_id) params.set("workspace_id", req.query.workspace_id);
  if (req.query.limit) params.set("limit", req.query.limit);
  const qs = params.toString();
  const path = qs ? `/analytics/users?${qs}` : "/analytics/users";
  proxyWorkspaceRequest(req, res, "GET", path);
});

app.get("/api/analytics/workspace/:id", (req, res) => {
  const params = new URLSearchParams();
  if (req.query.start_date) params.set("start_date", req.query.start_date);
  if (req.query.end_date) params.set("end_date", req.query.end_date);
  const qs = params.toString();
  const base = `/analytics/workspace/${encodeURIComponent(req.params.id)}`;
  proxyWorkspaceRequest(req, res, "GET", qs ? `${base}?${qs}` : base);
});

// ============================================================================
// Audit (chunk 05)
// ============================================================================
app.get("/api/audit/summary", (req, res) => {
  const params = new URLSearchParams();
  if (req.query.start_date) params.set("start_date", req.query.start_date);
  if (req.query.end_date) params.set("end_date", req.query.end_date);
  const qs = params.toString();
  const path = qs ? `/audit/summary?${qs}` : "/audit/summary";
  proxyWorkspaceRequest(req, res, "GET", path);
});

app.get("/api/audit/workspace/:id", (req, res) => {
  const params = new URLSearchParams();
  if (req.query.start_date) params.set("start_date", req.query.start_date);
  if (req.query.end_date) params.set("end_date", req.query.end_date);
  const qs = params.toString();
  const base = `/audit/workspace/${encodeURIComponent(req.params.id)}`;
  proxyWorkspaceRequest(req, res, "GET", qs ? `${base}?${qs}` : base);
});

app.get("/api/audit/events", (req, res) => {
  const params = new URLSearchParams();
  for (const k of ["start_date", "end_date", "workspace_id", "user_email",
                    "action", "category", "status", "cursor", "limit"]) {
    if (req.query[k]) params.set(k, req.query[k]);
  }
  const qs = params.toString();
  const path = qs ? `/audit/events?${qs}` : "/audit/events";
  proxyWorkspaceRequest(req, res, "GET", path);
});

app.get("/api/audit/event/:wsId/:requestId", (req, res) => {
  const path = `/audit/event/${encodeURIComponent(req.params.wsId)}/${encodeURIComponent(req.params.requestId)}`;
  proxyWorkspaceRequest(req, res, "GET", path);
});

// Debug: Show current authenticated user identity
app.get("/api/me", (req, res) => {
  const user = extractUser(req);
  const oidcData = req.headers["x-amzn-oidc-data"];
  let claims = {};
  if (oidcData) {
    try {
      claims = JSON.parse(Buffer.from(oidcData.split(".")[1], "base64url").toString());
    } catch { /* ignore */ }
  }
  res.json({ user, claims });
});

// ============================================================================
// Confluence Integration API (proxied to Confluence Lambda with SigV4)
// ============================================================================

// Mock Confluence data for local development
const mockConfluenceConnections = new Map();

// Mock Confluence handlers for local development
function handleMockConfluenceRequest(req, res, method, path) {
  const user = extractUser(req);
  const userEmail = user.email || "local@dev.com";
  console.log(`[mock] Confluence ${method} ${path} for: ${userEmail}`);

  const pathParts = path.split("/").filter(Boolean);

  // GET /confluence/authorize?workspace_id=... - Return mock OAuth URL
  if (method === "GET" && pathParts[0] === "confluence" && pathParts[1] === "authorize") {
    const workspaceId = req.query.workspace_id;
    if (!workspaceId) return res.status(400).json({ error: "workspace_id required" });
    // In local dev mode, return a mock URL that would normally redirect to Atlassian
    return res.json({
      auth_url: `http://localhost:3000/api/confluence/callback?code=mock_code&state=${encodeURIComponent(JSON.stringify({ workspace_id: workspaceId }))}`,
    });
  }

  // GET /confluence/callback - Mock OAuth callback
  if (method === "GET" && pathParts[0] === "confluence" && pathParts[1] === "callback") {
    const { code, state } = req.query;
    if (!code || !state) {
      return res.redirect("/?error=invalid_callback");
    }
    try {
      const stateData = JSON.parse(decodeURIComponent(state));
      const workspaceId = stateData.workspace_id;
      const connectionId = `conn-${Date.now()}`;

      // Create mock connection
      const connections = mockConfluenceConnections.get(workspaceId) || [];
      connections.push({
        connection_id: connectionId,
        workspace_id: workspaceId,
        cloud_id: "mock-cloud-id",
        site_name: "Mock Confluence Site",
        site_url: "https://mock.atlassian.net/wiki",
        selected_spaces: [],
        include_attachments: false,
        last_sync_at: null,
        last_sync_status: "needs_configuration",
        page_count: 0,
        attachment_count: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      });
      mockConfluenceConnections.set(workspaceId, connections);

      return res.redirect(`/workspaces/${workspaceId}/settings?tab=integrations&configure=${connectionId}`);
    } catch {
      return res.redirect("/?error=invalid_state");
    }
  }

  // GET /workspaces/:id/confluence/connections - List connections
  if (
    method === "GET" &&
    pathParts[0] === "workspaces" &&
    pathParts[2] === "confluence" &&
    pathParts[3] === "connections" &&
    pathParts.length === 4
  ) {
    const connections = mockConfluenceConnections.get(pathParts[1]) || [];
    return res.json({ connections });
  }

  // GET /workspaces/:id/confluence/connections/:connId/spaces - List spaces
  if (
    method === "GET" &&
    pathParts[0] === "workspaces" &&
    pathParts[2] === "confluence" &&
    pathParts[3] === "connections" &&
    pathParts[5] === "spaces"
  ) {
    // Return mock spaces
    return res.json({
      spaces: [
        { id: "space-1", key: "ENG", name: "Engineering", type: "global", selected: false },
        { id: "space-2", key: "PROD", name: "Product", type: "global", selected: false },
        { id: "space-3", key: "DOC", name: "Documentation", type: "global", selected: false },
      ],
      selected_count: 0,
    });
  }

  // PUT /workspaces/:id/confluence/connections/:connId/spaces - Update spaces
  if (
    method === "PUT" &&
    pathParts[0] === "workspaces" &&
    pathParts[2] === "confluence" &&
    pathParts[3] === "connections" &&
    pathParts[5] === "spaces"
  ) {
    const { selected_spaces } = req.body;
    const workspaceId = pathParts[1];
    const connectionId = pathParts[4];
    const connections = mockConfluenceConnections.get(workspaceId) || [];
    const conn = connections.find(c => c.connection_id === connectionId);
    if (conn) {
      conn.selected_spaces = selected_spaces;
      conn.last_sync_status = "queued";
      conn.updated_at = new Date().toISOString();
    }
    return res.json({ message: "Spaces updated", selected_count: selected_spaces.length });
  }

  // POST /workspaces/:id/confluence/connections/:connId/sync - Trigger sync
  if (
    method === "POST" &&
    pathParts[0] === "workspaces" &&
    pathParts[2] === "confluence" &&
    pathParts[3] === "connections" &&
    pathParts[5] === "sync"
  ) {
    const workspaceId = pathParts[1];
    const connectionId = pathParts[4];
    const connections = mockConfluenceConnections.get(workspaceId) || [];
    const conn = connections.find(c => c.connection_id === connectionId);
    if (conn) {
      conn.last_sync_status = "queued";
    }
    return res.json({ message: "Sync queued", status: "queued" });
  }

  // DELETE /workspaces/:id/confluence/connections/:connId - Delete connection
  if (
    method === "DELETE" &&
    pathParts[0] === "workspaces" &&
    pathParts[2] === "confluence" &&
    pathParts[3] === "connections" &&
    pathParts.length === 5
  ) {
    const workspaceId = pathParts[1];
    const connectionId = pathParts[4];
    const connections = mockConfluenceConnections.get(workspaceId) || [];
    const filtered = connections.filter(c => c.connection_id !== connectionId);
    mockConfluenceConnections.set(workspaceId, filtered);
    return res.json({ message: "Connection deleted" });
  }

  return res.status(404).json({ error: "Not found" });
}

// Helper to proxy Confluence requests
async function proxyConfluenceRequest(req, res, method, path) {
  // Use mock handlers in local development mode
  if (LOCAL_DEV_MODE) {
    return handleMockConfluenceRequest(req, res, method, path);
  }

  if (!CONFLUENCE_LAMBDA_URL) {
    console.log("[proxy] CONFLUENCE_LAMBDA_URL not configured - Confluence integration disabled");
    res.status(404).json({ error: "Confluence integration not enabled. Set CONFLUENCE_LAMBDA_URL or LOCAL_DEV_MODE=true." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Confluence ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedConfluenceFetch(method, path, options);

    // Handle redirects (OAuth callback)
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Handle non-OK responses
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Confluence Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "Confluence request failed" });
      return;
    }

    // Parse JSON response safely
    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from Confluence Lambda");
      res.status(500).json({ error: "Empty response from Confluence service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse Confluence response: ${text}`);
      res.status(500).json({ error: "Invalid response from Confluence service" });
    }
  } catch (error) {
    console.error(`[proxy] Confluence error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process Confluence request: ${error.message}` });
    }
  }
}

// Confluence OAuth routes
app.get("/api/confluence/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxyConfluenceRequest(req, res, "GET", `/confluence/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/confluence/callback", async (req, res) => {
  // OAuth callback - pass params in body to avoid query string SigV4 issues
  const { code, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  try {
    // POST the OAuth params to Lambda instead of GET with query string
    const response = await signedConfluenceFetch("POST", "/confluence/callback", {
      body: JSON.stringify({ code, state }),
    });

    // Handle redirect response
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Try to get redirect from body
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] Confluence callback error: ${error.message}`);
    res.redirect(`/?error=callback_failed`);
  }
});

// Confluence connections
app.get("/api/workspaces/:id/confluence/connections", (req, res) => {
  proxyConfluenceRequest(req, res, "GET", `/workspaces/${req.params.id}/confluence/connections`);
});

// Confluence spaces
app.get("/api/workspaces/:id/confluence/connections/:connId/spaces", (req, res) => {
  proxyConfluenceRequest(req, res, "GET", `/workspaces/${req.params.id}/confluence/connections/${req.params.connId}/spaces`);
});
app.put("/api/workspaces/:id/confluence/connections/:connId/spaces", (req, res) => {
  proxyConfluenceRequest(req, res, "PUT", `/workspaces/${req.params.id}/confluence/connections/${req.params.connId}/spaces`);
});

// Confluence sync
app.post("/api/workspaces/:id/confluence/connections/:connId/sync", (req, res) => {
  proxyConfluenceRequest(req, res, "POST", `/workspaces/${req.params.id}/confluence/connections/${req.params.connId}/sync`);
});

// Delete Confluence connection
app.delete("/api/workspaces/:id/confluence/connections/:connId", (req, res) => {
  proxyConfluenceRequest(req, res, "DELETE", `/workspaces/${req.params.id}/confluence/connections/${req.params.connId}`);
});

// ============================================================================
// Jira Integration Routes
// ============================================================================

// Helper to proxy Jira requests
async function proxyJiraRequest(req, res, method, path) {
  if (!JIRA_LAMBDA_URL) {
    console.log("[proxy] JIRA_LAMBDA_URL not configured - Jira integration disabled");
    res.status(404).json({ error: "Jira integration not enabled. Set JIRA_LAMBDA_URL." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Jira ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedJiraFetch(method, path, options);

    // Handle redirects (OAuth callback)
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Handle non-OK responses
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Jira Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "Jira request failed" });
      return;
    }

    // Parse JSON response safely
    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from Jira Lambda");
      res.status(500).json({ error: "Empty response from Jira service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse Jira response: ${text}`);
      res.status(500).json({ error: "Invalid response from Jira service" });
    }
  } catch (error) {
    console.error(`[proxy] Jira error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process Jira request: ${error.message}` });
    }
  }
}

// Jira OAuth routes
app.get("/api/jira/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxyJiraRequest(req, res, "GET", `/jira/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/jira/callback", async (req, res) => {
  // OAuth callback - pass params in body to avoid query string SigV4 issues
  const { code, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  if (!JIRA_LAMBDA_URL) {
    return res.redirect(`/?error=jira_not_configured`);
  }

  try {
    // POST the OAuth params to Lambda instead of GET with query string
    const response = await signedJiraFetch("POST", "/jira/callback", {
      body: JSON.stringify({ code, state }),
    });

    // Handle redirect response
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Try to get redirect from body
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] Jira callback error: ${error.message}`);
    res.redirect(`/?error=jira_callback_failed`);
  }
});

// Jira connections
app.get("/api/workspaces/:id/jira/connections", (req, res) => {
  proxyJiraRequest(req, res, "GET", `/workspaces/${req.params.id}/jira/connections`);
});

// Jira projects
app.get("/api/workspaces/:id/jira/connections/:connId/projects", (req, res) => {
  proxyJiraRequest(req, res, "GET", `/workspaces/${req.params.id}/jira/connections/${req.params.connId}/projects`);
});
app.put("/api/workspaces/:id/jira/connections/:connId/projects", (req, res) => {
  proxyJiraRequest(req, res, "PUT", `/workspaces/${req.params.id}/jira/connections/${req.params.connId}/projects`);
});

// Jira sync
app.post("/api/workspaces/:id/jira/connections/:connId/sync", (req, res) => {
  proxyJiraRequest(req, res, "POST", `/workspaces/${req.params.id}/jira/connections/${req.params.connId}/sync`);
});

// Delete Jira connection
app.delete("/api/workspaces/:id/jira/connections/:connId", (req, res) => {
  proxyJiraRequest(req, res, "DELETE", `/workspaces/${req.params.id}/jira/connections/${req.params.connId}`);
});

// ============================================================================
// GitHub Integration Routes
// ============================================================================

// Helper to proxy GitHub requests
async function proxyGitHubRequest(req, res, method, path) {
  if (!GITHUB_LAMBDA_URL) {
    console.log("[proxy] GITHUB_LAMBDA_URL not configured - GitHub integration disabled");
    res.status(404).json({ error: "GitHub integration not enabled. Set GITHUB_LAMBDA_URL." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] GitHub ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedGitHubFetch(method, path, options);

    // Handle redirects (OAuth callback)
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Handle non-OK responses
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] GitHub Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "GitHub request failed" });
      return;
    }

    // Parse JSON response safely
    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from GitHub Lambda");
      res.status(500).json({ error: "Empty response from GitHub service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse GitHub response: ${text}`);
      res.status(500).json({ error: "Invalid response from GitHub service" });
    }
  } catch (error) {
    console.error(`[proxy] GitHub error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process GitHub request: ${error.message}` });
    }
  }
}

// GitHub OAuth/App install routes
app.get("/api/github/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxyGitHubRequest(req, res, "GET", `/github/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/github/callback", async (req, res) => {
  // GitHub App installation callback
  const { installation_id, setup_action, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  if (!GITHUB_LAMBDA_URL) {
    return res.redirect(`/?error=github_not_configured`);
  }

  try {
    // POST the callback params to Lambda
    const response = await signedGitHubFetch("POST", "/github/callback", {
      body: JSON.stringify({ installation_id, setup_action, state }),
    });

    // Handle redirect response
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Try to get redirect from body
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] GitHub callback error: ${error.message}`);
    res.redirect(`/?error=github_callback_failed`);
  }
});

// GitHub installations for workspace
app.get("/api/workspaces/:id/github/installations", (req, res) => {
  proxyGitHubRequest(req, res, "GET", `/workspaces/${req.params.id}/github/installations`);
});

// List repos available in an installation
app.get("/api/workspaces/:id/github/installations/:instId/repos", (req, res) => {
  proxyGitHubRequest(req, res, "GET", `/workspaces/${req.params.id}/github/installations/${req.params.instId}/repos`);
});

// GitHub repos for workspace
app.get("/api/workspaces/:id/github/repos", (req, res) => {
  proxyGitHubRequest(req, res, "GET", `/workspaces/${req.params.id}/github/repos`);
});
app.post("/api/workspaces/:id/github/repos", (req, res) => {
  proxyGitHubRequest(req, res, "POST", `/workspaces/${req.params.id}/github/repos`);
});
app.put("/api/workspaces/:id/github/repos/*", (req, res) => {
  // Extract repo name from wildcard (supports slashes like "owner/repo")
  const repoName = req.params[0];
  proxyGitHubRequest(req, res, "PUT", `/workspaces/${req.params.id}/github/repos/${encodeURIComponent(repoName)}`);
});
app.delete("/api/workspaces/:id/github/repos/*", (req, res) => {
  // Extract repo name from wildcard (supports slashes like "owner/repo")
  const repoName = req.params[0];
  proxyGitHubRequest(req, res, "DELETE", `/workspaces/${req.params.id}/github/repos/${encodeURIComponent(repoName)}`);
});

// GitHub sync
app.post("/api/workspaces/:id/github/sync", (req, res) => {
  proxyGitHubRequest(req, res, "POST", `/workspaces/${req.params.id}/github/sync`);
});

// ============================================================================
// Outlook Integration Routes
// ============================================================================

// Helper to proxy Outlook requests
async function proxyOutlookRequest(req, res, method, path) {
  if (!OUTLOOK_LAMBDA_URL) {
    console.log("[proxy] OUTLOOK_LAMBDA_URL not configured - Outlook integration disabled");
    res.status(404).json({ error: "Outlook integration not enabled. Set OUTLOOK_LAMBDA_URL." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Outlook ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedOutlookFetch(method, path, options);

    // Handle redirects (OAuth callback)
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Handle non-OK responses
    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Outlook Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "Outlook request failed" });
      return;
    }

    // Parse JSON response safely
    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from Outlook Lambda");
      res.status(500).json({ error: "Empty response from Outlook service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse Outlook response: ${text}`);
      res.status(500).json({ error: "Invalid response from Outlook service" });
    }
  } catch (error) {
    console.error(`[proxy] Outlook error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process Outlook request: ${error.message}` });
    }
  }
}

// Outlook OAuth routes
app.get("/api/outlook/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxyOutlookRequest(req, res, "GET", `/outlook/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/outlook/callback", async (req, res) => {
  // OAuth callback - pass params in body to avoid query string SigV4 issues
  const { code, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  if (!OUTLOOK_LAMBDA_URL) {
    return res.redirect(`/?error=outlook_not_configured`);
  }

  try {
    // POST the OAuth params to Lambda instead of GET with query string
    const response = await signedOutlookFetch("POST", "/outlook/callback", {
      body: JSON.stringify({ code, state }),
    });

    // Handle redirect response
    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    // Try to get redirect from body
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] Outlook callback error: ${error.message}`);
    res.redirect(`/?error=outlook_callback_failed`);
  }
});

// Outlook connections
app.get("/api/workspaces/:id/outlook/connections", (req, res) => {
  proxyOutlookRequest(req, res, "GET", `/workspaces/${req.params.id}/outlook/connections`);
});

// Outlook folders
app.get("/api/workspaces/:id/outlook/connections/:connId/folders", (req, res) => {
  proxyOutlookRequest(req, res, "GET", `/workspaces/${req.params.id}/outlook/connections/${req.params.connId}/folders`);
});
app.put("/api/workspaces/:id/outlook/connections/:connId/folders", (req, res) => {
  proxyOutlookRequest(req, res, "PUT", `/workspaces/${req.params.id}/outlook/connections/${req.params.connId}/folders`);
});

// Outlook sync
app.post("/api/workspaces/:id/outlook/connections/:connId/sync", (req, res) => {
  proxyOutlookRequest(req, res, "POST", `/workspaces/${req.params.id}/outlook/connections/${req.params.connId}/sync`);
});

// Delete Outlook connection
app.delete("/api/workspaces/:id/outlook/connections/:connId", (req, res) => {
  proxyOutlookRequest(req, res, "DELETE", `/workspaces/${req.params.id}/outlook/connections/${req.params.connId}`);
});

// ============================================================================
// Teams Integration Routes
// ============================================================================

// Helper to proxy Teams requests
async function proxyTeamsRequest(req, res, method, path) {
  if (!TEAMS_LAMBDA_URL) {
    console.log("[proxy] TEAMS_LAMBDA_URL not configured - Teams integration disabled");
    res.status(404).json({ error: "Teams integration not enabled. Set TEAMS_LAMBDA_URL." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] Teams ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedTeamsFetch(method, path, options);

    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] Teams Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "Teams request failed" });
      return;
    }

    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from Teams Lambda");
      res.status(500).json({ error: "Empty response from Teams service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse Teams response: ${text}`);
      res.status(500).json({ error: "Invalid response from Teams service" });
    }
  } catch (error) {
    console.error(`[proxy] Teams error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process Teams request: ${error.message}` });
    }
  }
}

// Teams OAuth routes
app.get("/api/teams/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxyTeamsRequest(req, res, "GET", `/teams/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/teams/callback", async (req, res) => {
  const { code, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  if (!TEAMS_LAMBDA_URL) {
    return res.redirect(`/?error=teams_not_configured`);
  }

  try {
    const response = await signedTeamsFetch("POST", "/teams/callback", {
      body: JSON.stringify({ code, state }),
    });

    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] Teams callback error: ${error.message}`);
    res.redirect(`/?error=teams_callback_failed`);
  }
});

// Teams connections
app.get("/api/workspaces/:id/teams/connections", (req, res) => {
  proxyTeamsRequest(req, res, "GET", `/workspaces/${req.params.id}/teams/connections`);
});

// Teams - list teams
app.get("/api/workspaces/:id/teams/connections/:connId/teams", (req, res) => {
  proxyTeamsRequest(req, res, "GET", `/workspaces/${req.params.id}/teams/connections/${req.params.connId}/teams`);
});

// Teams - list channels
app.get("/api/workspaces/:id/teams/connections/:connId/channels", (req, res) => {
  const teamId = req.query.team_id;
  proxyTeamsRequest(req, res, "GET", `/workspaces/${req.params.id}/teams/connections/${req.params.connId}/channels?team_id=${encodeURIComponent(teamId)}`);
});

// Teams - save config
app.put("/api/workspaces/:id/teams/connections/:connId/config", (req, res) => {
  proxyTeamsRequest(req, res, "PUT", `/workspaces/${req.params.id}/teams/connections/${req.params.connId}/config`);
});

// Teams sync
app.post("/api/workspaces/:id/teams/connections/:connId/sync", (req, res) => {
  proxyTeamsRequest(req, res, "POST", `/workspaces/${req.params.id}/teams/connections/${req.params.connId}/sync`);
});

// Delete Teams connection
app.delete("/api/workspaces/:id/teams/connections/:connId", (req, res) => {
  proxyTeamsRequest(req, res, "DELETE", `/workspaces/${req.params.id}/teams/connections/${req.params.connId}`);
});

// ============================================================================
// SharePoint Integration Routes
// ============================================================================

// Helper to proxy SharePoint requests
async function proxySharePointRequest(req, res, method, path) {
  if (!SHAREPOINT_LAMBDA_URL) {
    console.log("[proxy] SHAREPOINT_LAMBDA_URL not configured - SharePoint integration disabled");
    res.status(404).json({ error: "SharePoint integration not enabled. Set SHAREPOINT_LAMBDA_URL." });
    return;
  }

  const user = extractUser(req);
  console.log(`[proxy] SharePoint ${method} ${path} for: ${user.email}`);

  try {
    const options = {
      extraHeaders: userHeaders(req, user),
    };
    if (req.body && Object.keys(req.body).length > 0) {
      options.body = JSON.stringify(req.body);
    }

    const response = await signedSharePointFetch(method, path, options);

    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[proxy] SharePoint Lambda error: ${response.status} - ${errorText}`);
      res.status(response.status).json({ error: errorText || "SharePoint request failed" });
      return;
    }

    const text = await response.text();
    if (!text) {
      console.error("[proxy] Empty response from SharePoint Lambda");
      res.status(500).json({ error: "Empty response from SharePoint service" });
      return;
    }

    try {
      const data = JSON.parse(text);
      res.status(response.status).json(data);
    } catch (parseError) {
      console.error(`[proxy] Failed to parse SharePoint response: ${text}`);
      res.status(500).json({ error: "Invalid response from SharePoint service" });
    }
  } catch (error) {
    console.error(`[proxy] SharePoint error: ${error.message}`);
    if (error.message.includes("credentials") || error.message.includes("Credential")) {
      res.status(500).json({ error: "AWS credentials not configured." });
    } else {
      res.status(500).json({ error: `Failed to process SharePoint request: ${error.message}` });
    }
  }
}

// SharePoint OAuth routes
app.get("/api/sharepoint/authorize", (req, res) => {
  const workspaceId = req.query.workspace_id;
  proxySharePointRequest(req, res, "GET", `/sharepoint/authorize?workspace_id=${encodeURIComponent(workspaceId)}`);
});
app.get("/api/sharepoint/callback", async (req, res) => {
  const { code, state, error } = req.query;

  if (error) {
    return res.redirect(`/?error=${encodeURIComponent(error)}`);
  }

  if (!SHAREPOINT_LAMBDA_URL) {
    return res.redirect(`/?error=sharepoint_not_configured`);
  }

  try {
    const response = await signedSharePointFetch("POST", "/sharepoint/callback", {
      body: JSON.stringify({ code, state }),
    });

    if (response.status === 302) {
      const location = response.headers.get("location");
      return res.redirect(location);
    }

    const text = await response.text();
    try {
      const data = JSON.parse(text);
      if (data.redirect) {
        return res.redirect(data.redirect);
      }
      res.status(response.status).json(data);
    } catch {
      res.status(response.status).send(text);
    }
  } catch (error) {
    console.error(`[proxy] SharePoint callback error: ${error.message}`);
    res.redirect(`/?error=sharepoint_callback_failed`);
  }
});

// SharePoint connections
app.get("/api/workspaces/:id/sharepoint/connections", (req, res) => {
  proxySharePointRequest(req, res, "GET", `/workspaces/${req.params.id}/sharepoint/connections`);
});

// SharePoint - list sites
app.get("/api/workspaces/:id/sharepoint/connections/:connId/sites", (req, res) => {
  proxySharePointRequest(req, res, "GET", `/workspaces/${req.params.id}/sharepoint/connections/${req.params.connId}/sites`);
});

// SharePoint - update sites
app.put("/api/workspaces/:id/sharepoint/connections/:connId/sites", (req, res) => {
  proxySharePointRequest(req, res, "PUT", `/workspaces/${req.params.id}/sharepoint/connections/${req.params.connId}/sites`);
});

// SharePoint sync
app.post("/api/workspaces/:id/sharepoint/connections/:connId/sync", (req, res) => {
  proxySharePointRequest(req, res, "POST", `/workspaces/${req.params.id}/sharepoint/connections/${req.params.connId}/sync`);
});

// Delete SharePoint connection
app.delete("/api/workspaces/:id/sharepoint/connections/:connId", (req, res) => {
  proxySharePointRequest(req, res, "DELETE", `/workspaces/${req.params.id}/sharepoint/connections/${req.params.connId}`);
});

// ============================================================================
// Logout (clear ALB OIDC session cookies)
// ============================================================================

function clearSessionCookies(res) {
  // ALB OIDC sets HttpOnly session cookies - clear them by setting to expired
  // ALB may use multiple cookie segments (AWSELBAuthSessionCookie-0, -1, etc.)
  for (let i = 0; i < 5; i++) {
    res.cookie(`AWSELBAuthSessionCookie-${i}`, "", {
      expires: new Date(0),
      path: "/",
      httpOnly: true,
      secure: true,
    });
  }

  // Clear scanner session cookie (JWT used by security scanner / service accounts)
  res.cookie("oo_scanner_session", "", {
    expires: new Date(0),
    path: "/",
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
  });
}

app.get("/api/logout", (req, res) => {
  console.log("[logout] Clearing session cookies");
  clearSessionCookies(res);
  // Redirect to root - ALB will force re-authentication
  res.redirect("/");
});

app.post("/api/logout", (req, res) => {
  console.log("[logout] Clearing session cookies (POST)");
  clearSessionCookies(res);
  res.status(200).json({ message: "Logged out" });
});

// Serve static files with SPA fallback
app.use((req, res) => {
  // Get the requested path
  let filePath = join(STATIC_DIR, req.path);

  // Check if file exists
  if (existsSync(filePath) && !filePath.endsWith("/")) {
    const ext = extname(filePath);
    const mimeType = MIME_TYPES[ext] || "application/octet-stream";

    // Set cache headers for assets
    if (req.path.startsWith("/assets/")) {
      res.setHeader("Cache-Control", "public, max-age=31536000, immutable");
    }

    res.setHeader("Content-Type", mimeType);
    res.send(readFileSync(filePath));
  } else {
    // SPA fallback - serve index.html for all routes
    const indexPath = join(STATIC_DIR, "index.html");
    if (existsSync(indexPath)) {
      res.setHeader("Content-Type", "text/html");
      res.send(readFileSync(indexPath));
    } else {
      res.status(404).send("Not found");
    }
  }
});

// Load scanner secret then start server
loadScannerSecret().then(() => {
  app.listen(PORT, () => {
    console.log(`[server] OutcomeOps UI server listening on port ${PORT}`);
    console.log(`[server] Static files: ${STATIC_DIR}`);
    console.log(`[server] Chat Lambda URL: ${LAMBDA_FUNCTION_URL ? "configured" : "NOT CONFIGURED"}`);
    console.log(`[server] Workspace Lambda URL: ${WORKSPACE_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] Confluence Lambda URL: ${CONFLUENCE_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] Jira Lambda URL: ${JIRA_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] GitHub Lambda URL: ${GITHUB_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] Outlook Lambda URL: ${OUTLOOK_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] Teams Lambda URL: ${TEAMS_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] SharePoint Lambda URL: ${SHAREPOINT_LAMBDA_URL ? "configured" : "disabled"}`);
    console.log(`[server] Scanner auth: ${scannerJwtSecret ? "enabled" : "disabled"}`);
    console.log(`[server] Local dev mode: ${LOCAL_DEV_MODE ? "ENABLED (using mock data)" : "disabled"}`);
    console.log(`[server] AWS Region: ${AWS_REGION}`);
  });
});
