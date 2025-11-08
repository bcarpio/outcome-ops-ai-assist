environment = "prd"

# Configure which repositories to ingest into the knowledge base
# The allowlist is automatically stored in SSM Parameter Store
# and loaded by the ingest-docs Lambda handler at runtime
repos_to_ingest = [
  {
    name    = "outcome-ops-ai-assist"
    project = "bcarpio/outcome-ops-ai-assist"
    type    = "application"
  },
  {
    name    = "fantacyai-adrs"
    project = "bcarpio/fantacyai-adrs"
    type    = "standards"
  },
  {
    name    = "fantacyai-ui"
    project = "bcarpio/fantacyai-ui"
    type    = "application"
  },
  {
    name    = "fantacyai-api-aws"
    project = "bcarpio/fantacyai-api-aws"
    type    = "application"
  }
]
