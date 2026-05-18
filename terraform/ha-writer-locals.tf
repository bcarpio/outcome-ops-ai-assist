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
  # Env vars + IAM merged into every scheduler Lambda's module. Reads
  # the local-region AppConfig role and short-circuits if the region is
  # passive. The Python-side gate uses shared.active_region.is_active_region().
  # Single-region deploys (var.secondary_region == "") get empty values
  # here, which makes is_active_region() return True (no AppConfig call).
  ha_scheduler_env = var.secondary_region != "" ? {
    APPCONFIG_APPLICATION_ID           = aws_appconfig_application.ha.id
    APPCONFIG_ENVIRONMENT_ID           = aws_appconfig_environment.ha.environment_id
    APPCONFIG_CONFIGURATION_PROFILE_ID = aws_appconfig_configuration_profile.ha_active_region.configuration_profile_id
    THIS_REGION                        = var.aws_region
  } : {}

  ha_scheduler_iam = var.secondary_region != "" ? {
    ha_appconfig_read = {
      effect = "Allow"
      actions = [
        "appconfig:StartConfigurationSession",
        "appconfig:GetLatestConfiguration",
      ]
      resources = [
        "arn:aws:appconfig:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application/${aws_appconfig_application.ha.id}/environment/${aws_appconfig_environment.ha.environment_id}/configuration/${aws_appconfig_configuration_profile.ha_active_region.configuration_profile_id}",
      ]
    }
  } : {}

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
