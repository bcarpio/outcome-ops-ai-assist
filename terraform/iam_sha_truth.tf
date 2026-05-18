# ============================================================================
# Shared local for SHA-truth table ARNs (plan-git-truth chunk 01).
#
# The pending-work ledger and failed-files tables are written by every
# pipeline Lambda that participates in the SHA-truth contract:
#   - github-sync             (dispatcher: docs + code_map pending rows)
#   - process-repo-summary    (dispatcher: per-code-unit code_map ledger)
#   - process-batch-summary   (dispatcher + consumer: gkg ledger)
#   - file-ingestion          (consumer: docs ledger)
#
# Resource ARNs are exact (per ADR-001 — no wildcards). The list is
# defined here once so each Lambda's policy_statements block references
# local.sha_truth_table_arns instead of duplicating the two ARNs.
# ============================================================================

locals {
  sha_truth_table_arns = [
    module.code_map_pending_work_table.dynamodb_table_arn,
    module.code_map_failed_files_table.dynamodb_table_arn,
  ]

  # Secondary-region parallels for HA writer Lambdas (chunk 9e). Empty
  # when secondary_region is unset; populated otherwise.
  sha_truth_table_arns_secondary = var.secondary_region != "" ? [
    module.code_map_pending_work_table_secondary[0].dynamodb_table_arn,
    module.code_map_failed_files_table_secondary[0].dynamodb_table_arn,
  ] : []
}
