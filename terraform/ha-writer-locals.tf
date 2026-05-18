# ============================================================================
# HA writer shared locals
# ----------------------------------------------------------------------------
# Every writer Lambda that should emit cross-region replication events
# merges these env vars into its environment_variables block, and merges
# the policy_statement into its IAM. Centralizing here keeps the 12
# writer Lambdas' lambda-*.tf files consistent — when chunk 04's event
# schema or the bus name pattern changes, we update one file.
#
# In single-region mode (var.secondary_region == ""):
# - REPLICATION_EVENT_BUS_NAME is "" so shared.dual_write._emit short-
#   circuits without any boto3 call.
# - The events:PutEvents IAM grant has an empty resources list, which
#   terraform renders as a no-op statement.
#
# In HA mode (var.secondary_region set):
# - Lambdas in the primary region emit to this region's replication bus.
#   The cross-region rule (chunk 03) forwards events to the secondary's
#   bus; the secondary's applier (chunk 05) replays them locally.
# - Lambdas deployed under aws.secondary would consume their own region's
#   bus name + region — see the per-Lambda secondary-deploy patterns in
#   the lambda-*.tf files where applicable.
# ============================================================================

locals {
  # Env vars merged into every writer Lambda's environment_variables block.
  # Lambdas reading this block don't need to know about HA at all — the
  # presence of REPLICATION_EVENT_BUS_NAME triggers shared.dual_write to
  # emit; absence means single-region.
  ha_writer_env = {
    REPLICATION_EVENT_BUS_NAME = var.secondary_region != "" ? aws_cloudwatch_event_bus.replication.name : ""
    THIS_REGION                = var.aws_region
    PRIMARY_REGION             = var.aws_region
    SECONDARY_REGION           = var.secondary_region
  }

  # IAM policy statement merged into every writer Lambda's policy_statements
  # map. Grants events:PutEvents on the local replication bus ONLY when
  # secondary_region is set; otherwise the resources list is empty (no-op).
  ha_writer_events_iam = var.secondary_region != "" ? {
    ha_events_putevents = {
      effect    = "Allow"
      actions   = ["events:PutEvents"]
      resources = [aws_cloudwatch_event_bus.replication.arn]
    }
  } : {}
}
