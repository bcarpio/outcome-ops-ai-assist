/**
 * Chunk 13: shared testable middleware factories.
 *
 * Extracted from index.js so vitest can exercise them without loading the
 * 2600-line launcher (which binds a port + loads SSM at import time).
 * index.js imports the same factories, so behavior stays identical.
 */

import jwt from "jsonwebtoken";

/**
 * Routes that are safe to hit while unauthenticated.
 *
 * /api/logout only clears cookies -- an already-expired session hitting
 * the auth gate would 401, leaving stale cookies and looping the user
 * through the ALB login.
 *
 * /api/region/role is deliberately unauthenticated at the Lambda so the
 * UI can pick which region to route to before the user is even signed in.
 */
export const AUTH_EXEMPT_PATHS = new Set([
  "/api/logout",
  "/api/region/role",
]);

/**
 * Extract user identity from ALB OIDC headers or scanner-session cookie.
 * Returns `{ email: "anonymous", ... }` when neither is present.
 */
export function makeExtractUser({ getScannerJwtSecret }) {
  return function extractUser(req) {
    const oidcData = req.headers["x-amzn-oidc-data"];
    if (oidcData) {
      try {
        const payload = JSON.parse(
          Buffer.from(oidcData.split(".")[1], "base64url").toString(),
        );
        let email = payload.email || payload.preferred_username || null;
        if (!email) {
          const accessToken = req.headers["x-amzn-oidc-accesstoken"];
          if (accessToken) {
            try {
              const tokenPayload = JSON.parse(
                Buffer.from(accessToken.split(".")[1], "base64url").toString(),
              );
              email = tokenPayload.upn || tokenPayload.preferred_username || tokenPayload.unique_name || null;
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

    const scannerToken = req.cookies?.oo_scanner_session;
    const secret = getScannerJwtSecret?.();
    if (scannerToken && secret) {
      try {
        const decoded = jwt.verify(scannerToken, secret);
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
  };
}

/**
 * Auth + content-type gate for the /api scope.
 * - Exempts AUTH_EXEMPT_PATHS from the anonymous-check so /api/logout
 *   clears cookies even for an expired session, and /api/region/role
 *   answers before login.
 * - Rejects non-JSON bodies with 415 so downstream handlers don't have
 *   to sanity-check content-type.
 */
export function makeApiAuthGate({ extractUser }) {
  return function apiAuthGate(req, res, next) {
    if (AUTH_EXEMPT_PATHS.has(req.path)) {
      req.user = extractUser(req);
      return next();
    }
    const user = extractUser(req);
    if (user.email === "anonymous") {
      return res.status(401).json({ error: "Authentication required" });
    }
    const ct = req.headers["content-type"] || "";
    if (req.method !== "GET" && req.method !== "DELETE" && ct && !ct.includes("application/json")) {
      return res.status(415).json({ error: "Unsupported Media Type. Use application/json" });
    }
    req.user = user;
    next();
  };
}

/**
 * /api 404 guard. Must sit AFTER every /api route, BEFORE the SPA fallback.
 * Without this, an unknown /api/* URL fell through to index.html with a
 * 200 status; frontend safeJson() turned that into null (silent empty
 * data instead of a diagnosable 404).
 */
export function apiNotFound(req, res) {
  res.status(404).json({ error: "not found", path: req.originalUrl });
}

/**
 * Terminal JSON error middleware. Catches body-parse failures (>1MB body
 * or malformed JSON) and thrown handler errors that bypassed try/catch.
 * Must sit AFTER all routes but BEFORE the static/SPA fallback.
 */
// eslint-disable-next-line no-unused-vars -- Express requires 4-arg signature
export function jsonErrorMiddleware(err, req, res, next) {
  const isBodyParseError = err.type === "entity.parse.failed" || err.type === "entity.too.large";
  const status = err.status || err.statusCode || (isBodyParseError ? 400 : 500);
  const body = { error: isBodyParseError ? "invalid JSON body" : "internal server error" };
  if (!res.headersSent) {
    res.status(status).json(body);
  }
  console.error(`[error-mw] ${req.method} ${req.originalUrl}: ${err.message}`);
}
