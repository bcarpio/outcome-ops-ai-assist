"""Salesforce Integration Lambda -- stub for chunk 01 terraform scaffolding.

The real handler ships in chunk 02: OAuth authorize/callback + connection
lifecycle for the deploy's shared Salesforce Connected App. Env-SSM
OAuth pattern matches Confluence/Jira: SALESFORCE_CLIENT_ID from env
(loaded from tfvar), SALESFORCE_CLIENT_SECRET_SSM_PARAM SSM SecureString
populated by the operator.
"""


def lambda_handler(event, context):
    return {
        "statusCode": 501,
        "body": '{"error": "salesforce-integration handler not yet implemented (chunk 02)"}',
    }
