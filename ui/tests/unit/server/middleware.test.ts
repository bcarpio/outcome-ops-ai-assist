// Copyright 2025-2026 OutcomeOps
// SPDX-License-Identifier: Apache-2.0
// @vitest-environment node
/**
 * first server-side tests. Exercises the auth gate, the /api
 * 404 guard, and the terminal JSON error middleware directly via
 * fake req/res objects (no supertest / no port bind) so this suite can
 * live inside the existing vitest project.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import jwt from "jsonwebtoken";
import {
  AUTH_EXEMPT_PATHS,
  makeExtractUser,
  makeApiAuthGate,
  apiNotFound,
  jsonErrorMiddleware,
} from "../../../server/middleware.js";

type MockRes = {
  statusCode: number;
  jsonBody: unknown;
  headersSent: boolean;
  status: (code: number) => MockRes;
  json: (body: unknown) => MockRes;
};

function fakeRes(): MockRes {
  const res: MockRes = {
    statusCode: 0,
    jsonBody: undefined,
    headersSent: false,
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(body) {
      this.jsonBody = body;
      this.headersSent = true;
      return this;
    },
  };
  return res;
}

// Build a valid ALB OIDC "JWT" (only the middle segment is decoded; the
// signature isn't checked because ALB already verified it).
function oidcJwtFor(payload: Record<string, unknown>): string {
  const header = Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${header}.${body}.`;
}

describe("makeExtractUser", () => {
  const extract = makeExtractUser({ getScannerJwtSecret: () => null });

  it("returns anonymous when no OIDC data present", () => {
    const req = { headers: {}, cookies: {} };
    expect(extract(req).email).toBe("anonymous");
  });

  it("extracts email from the OIDC data header (lowercased)", () => {
    const req = {
      headers: {
        "x-amzn-oidc-data": oidcJwtFor({
          email: "Alice@example.com",
          name: "Alice",
        }),
      },
      cookies: {},
    };
    const user = extract(req);
    expect(user.email).toBe("alice@example.com");
    expect(user.name).toBe("Alice");
  });

  it("falls back to upn in the access token when email is missing (Azure AD cloud-only)", () => {
    const req = {
      headers: {
        "x-amzn-oidc-data": oidcJwtFor({ sub: "sub-123" }),
        "x-amzn-oidc-accesstoken": oidcJwtFor({ upn: "Bob@corp.example.com" }),
      },
      cookies: {},
    };
    expect(extract(req).email).toBe("bob@corp.example.com");
  });

  it("returns anonymous on malformed OIDC data (never throws)", () => {
    const req = {
      headers: { "x-amzn-oidc-data": "not.a.jwt" },
      cookies: {},
    };
    const user = extract(req);
    // Malformed OIDC data yields the try/catch fallback of {unknown, unknown}
    // rather than propagating.
    expect(["anonymous", "unknown"]).toContain(user.email);
  });

  it("accepts a scanner session cookie when the secret is configured", () => {
    const secret = "test-secret-32bytes-1234567890abcd";
    const token = jwt.sign({ auth_type: "scanner", email: "scanner@corp" }, secret);
    const extractWithSecret = makeExtractUser({ getScannerJwtSecret: () => secret });
    const req = { headers: {}, cookies: { oo_scanner_session: token } };
    expect(extractWithSecret(req).email).toBe("scanner@corp");
  });

  it("ignores scanner cookie when the secret is missing", () => {
    const req = { headers: {}, cookies: { oo_scanner_session: "anything" } };
    expect(extract(req).email).toBe("anonymous");
  });
});

describe("makeApiAuthGate", () => {
  const extract = makeExtractUser({ getScannerJwtSecret: () => null });
  const gate = makeApiAuthGate({ extractUser: extract });

  it("401s an anonymous request", () => {
    const req = { path: "/api/anything", method: "GET", headers: {}, cookies: {} };
    const res = fakeRes();
    const next = vi.fn();
    gate(req, res, next);
    expect(res.statusCode).toBe(401);
    expect(res.jsonBody).toEqual({ error: "Authentication required" });
    expect(next).not.toHaveBeenCalled();
  });

  it("passes an authenticated request through", () => {
    const req = {
      path: "/api/workspaces",
      method: "GET",
      headers: { "x-amzn-oidc-data": oidcJwtFor({ email: "alice@example.com" }) },
      cookies: {},
    };
    const res = fakeRes();
    const next = vi.fn();
    gate(req, res, next);
    expect(res.statusCode).toBe(0);
    expect(next).toHaveBeenCalledTimes(1);
    expect((req as unknown as { user: { email: string } }).user.email).toBe("alice@example.com");
  });

  it("415s a non-JSON POST body from an authenticated user", () => {
    const req = {
      path: "/api/workspaces",
      method: "POST",
      headers: {
        "x-amzn-oidc-data": oidcJwtFor({ email: "alice@example.com" }),
        "content-type": "text/plain",
      },
      cookies: {},
    };
    const res = fakeRes();
    const next = vi.fn();
    gate(req, res, next);
    expect(res.statusCode).toBe(415);
    expect(next).not.toHaveBeenCalled();
  });

  it("exempts /api/logout so an expired session can still clear cookies", () => {
    // Reg: without the exemption, an anonymous logout returned 401 and left
    // stale cookies, looping the user through the ALB login page.
    const req = { path: "/api/logout", method: "GET", headers: {}, cookies: {} };
    const res = fakeRes();
    const next = vi.fn();
    gate(req, res, next);
    expect(res.statusCode).toBe(0);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it("exempts /api/region/role so the UI can route before login", () => {
    const req = { path: "/api/region/role", method: "GET", headers: {}, cookies: {} };
    const res = fakeRes();
    const next = vi.fn();
    gate(req, res, next);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it("AUTH_EXEMPT_PATHS covers both known routes", () => {
    expect(AUTH_EXEMPT_PATHS.has("/api/logout")).toBe(true);
    expect(AUTH_EXEMPT_PATHS.has("/api/region/role")).toBe(true);
    expect(AUTH_EXEMPT_PATHS.has("/api/workspaces")).toBe(false);
  });
});

describe("apiNotFound", () => {
  it("returns JSON 404 with the request path", () => {
    // Reg: pre-, unmatched /api/* fell through to the SPA fallback
    // and returned index.html with a 200. safeJson() then turned that
    // into null, silently emptying data.
    const req = { originalUrl: "/api/does-not-exist" };
    const res = fakeRes();
    apiNotFound(req as never, res as never);
    expect(res.statusCode).toBe(404);
    expect(res.jsonBody).toEqual({
      error: "not found",
      path: "/api/does-not-exist",
    });
  });
});

describe("jsonErrorMiddleware", () => {
  let errorSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    errorSpy.mockRestore();
  });

  it("returns JSON 400 on body-parse failure", () => {
    const err = Object.assign(new Error("Unexpected token"), {
      type: "entity.parse.failed",
    });
    const req = { method: "POST", originalUrl: "/api/x" };
    const res = fakeRes();
    jsonErrorMiddleware(err, req as never, res as never, () => {});
    expect(res.statusCode).toBe(400);
    expect(res.jsonBody).toEqual({ error: "invalid JSON body" });
  });

  it("returns JSON 400 on oversized body (entity.too.large)", () => {
    const err = Object.assign(new Error("too big"), { type: "entity.too.large" });
    const req = { method: "POST", originalUrl: "/api/x" };
    const res = fakeRes();
    jsonErrorMiddleware(err, req as never, res as never, () => {});
    expect(res.statusCode).toBe(400);
    expect(res.jsonBody).toEqual({ error: "invalid JSON body" });
  });

  it("returns JSON 500 on generic handler error", () => {
    const err = new Error("boom");
    const req = { method: "GET", originalUrl: "/api/x" };
    const res = fakeRes();
    jsonErrorMiddleware(err, req as never, res as never, () => {});
    expect(res.statusCode).toBe(500);
    expect(res.jsonBody).toEqual({ error: "internal server error" });
  });

  it("does not overwrite headers when they were already sent (streaming path)", () => {
    const err = new Error("mid-stream");
    const req = { method: "POST", originalUrl: "/api/chat" };
    const res = fakeRes();
    res.headersSent = true;
    jsonErrorMiddleware(err, req as never, res as never, () => {});
    // Middleware short-circuits when headers are already sent.
    expect(res.statusCode).toBe(0);
    expect(res.jsonBody).toBeUndefined();
  });
});
