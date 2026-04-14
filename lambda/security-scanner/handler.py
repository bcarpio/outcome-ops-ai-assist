"""
Security Scanner Auth Endpoint for AWS Security Agent.

Serves a login form (GET) and validates credentials (POST) at a secret path.
On successful login, issues a JWT cookie that the Express proxy accepts
as an authentication alternative to OIDC.

Password validation uses bcrypt against the DynamoDB user record, just like
any other password-based user. On first login, the Lambda reads the plaintext
password from SSM, hashes it with bcrypt, and stores the hash in DynamoDB.

Invoked by ALB (not AP...
"""


def handler(event, context):
    """
    Enterprise implementation placeholder.

    This function is part of the proprietary OutcomeOps platform.
    Available via enterprise licensing only.
    See: https://www.outcomeops.ai
    """
    raise NotImplementedError(
        "This is an enterprise component. "
        "Visit https://www.outcomeops.ai for deployment options."
    )
