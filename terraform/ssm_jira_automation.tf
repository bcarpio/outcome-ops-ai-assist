# Jira Automation via SSM
#
# Resources:
# - aws_ssm_document "jira_automation" (SSM Automation Document for Jira triggers)
# - module "atlassian_automation_role" (IAM role for Atlassian cross-account trust)
#
# Conditional on enable_jira_integration. Allows Jira Automation to
# trigger code generation via SSM Document, bypassing API Gateway.
# Flow: Jira Issue -> Jira Automation -> AWS (assume role) -> SSM -> Lambda.
# Supports approved-for-generation and approved-for-plan labels.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
