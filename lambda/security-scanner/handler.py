"""
Security Scanner Auth Endpoint for AWS Security Agent.

Serves a login form (GET) and validates credentials (POST) at a secret path.
On successful login, issues a JWT cookie that the Express proxy accepts
as an authentication alternative to OIDC.

Password validation uses bcrypt against the DynamoDB user record, just like
any other password-based user. On first login, the Lambda reads the plaintext
password from SSM, hashes it with bcrypt, and stores the hash in DynamoDB.

Invoked by ALB (not API Gateway or Function URL).
"""

import base64
import logging
import os
import time
from urllib.parse import parse_qs

import bcrypt
import boto3
import jwt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

# Cached per container
_jwt_secret = None

SCANNER_PATH = os.environ.get("SCANNER_PATH", "")
SCANNER_EMAIL = os.environ.get("SCANNER_EMAIL", "")
REDIRECT_URL = os.environ.get("REDIRECT_URL", "/workspaces")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE", "")


def get_jwt_secret():
    """Read and cache the JWT signing secret from SSM."""
    global _jwt_secret
    if _jwt_secret is not None:
        return _jwt_secret
    _jwt_secret = ssm.get_parameter(
        Name=os.environ["SSM_JWT_SECRET"], WithDecryption=True
    )["Parameter"]["Value"]
    return _jwt_secret


def _get_user(email):
    """Get user profile from DynamoDB."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    response = table.get_item(Key={"PK": f"USER#{email}", "SK": "PROFILE"})
    return response.get("Item")


def _validate_password(user, password):
    """Validate password against DynamoDB user record.

    On first login (empty password_hash), reads plaintext from SSM,
    hashes with bcrypt, stores the hash, then validates.
    """
    password_hash = user.get("password_hash", "")

    if not password_hash:
        # First login: hash the SSM password and store it
        plaintext = ssm.get_parameter(
            Name=os.environ["SSM_PASSWORD"], WithDecryption=True
        )["Parameter"]["Value"]
        password_hash = bcrypt.hashpw(
            plaintext.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        # Store hash in DynamoDB
        table = dynamodb.Table(WORKSPACES_TABLE)
        table.update_item(
            Key={"PK": user["PK"], "SK": user["SK"]},
            UpdateExpression="SET password_hash = :h",
            ExpressionAttributeValues={":h": password_hash},
        )
        logger.info("Stored bcrypt hash for %s on first login", user.get("user_email"))

    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _html_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html"},
        "body": body,
        "isBase64Encoded": False,
    }


def _login_form(error=""):
    error_html = f'<p style="color:red">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head><title>Security Test Login</title></head>
<body>
  <h1>Security Test Login</h1>
  {error_html}
  <form method="POST">
    <label for="email">Email</label><br>
    <input type="email" id="email" name="email" required><br><br>
    <label for="password">Password</label><br>
    <input type="password" id="password" name="password" required><br><br>
    <button type="submit">Sign In</button>
  </form>
</body>
</html>"""


def _validate_path(path):
    """Check that the request path contains the secret scanner path."""
    expected = f"/auth/{SCANNER_PATH}"
    return path.rstrip("/") == expected.rstrip("/")


def handle_get(path):
    if not _validate_path(path):
        return _html_response(404, "Not Found")
    return _html_response(200, _login_form())


def handle_post(path, body, is_base64):
    if not _validate_path(path):
        return _html_response(404, "Not Found")

    if is_base64:
        body = base64.b64decode(body).decode("utf-8")

    params = parse_qs(body)
    email = params.get("email", [""])[0]
    password = params.get("password", [""])[0]

    if not email or not password:
        return _html_response(200, _login_form("Email and password are required."))

    # Look up user in DynamoDB
    user = _get_user(email)
    if not user or user.get("auth_type") != "password":
        logger.warning("Scanner login failed: user not found or wrong auth_type")
        return _html_response(200, _login_form("Invalid credentials."))

    # Validate password against DynamoDB hash
    if not _validate_password(user, password):
        logger.warning("Scanner login failed: password mismatch for %s", email)
        return _html_response(200, _login_form("Invalid credentials."))

    # Issue JWT
    jwt_secret = get_jwt_secret()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": email,
            "email": email,
            "name": user.get("name", "Security Scanner"),
            "auth_type": "scanner",
            "iat": now,
            "exp": now + 3600,
        },
        jwt_secret,
        algorithm="HS256",
    )

    logger.info("Scanner login successful for %s", email)

    return {
        "statusCode": 302,
        "headers": {
            "Location": REDIRECT_URL,
            "Set-Cookie": f"oo_scanner_session={token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=3600",
        },
        "body": "",
        "isBase64Encoded": False,
    }


def lambda_handler(event, context):
    method = event.get("httpMethod", "GET")
    path = event.get("path", "")

    if method == "GET":
        return handle_get(path)
    elif method == "POST":
        body = event.get("body", "")
        is_base64 = event.get("isBase64Encoded", False)
        return handle_post(path, body, is_base64)
    else:
        return _html_response(405, "Method Not Allowed")
