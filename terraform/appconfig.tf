# ============================================================================
# AWS AppConfig: per-region active/passive role flag for HA failover
# ----------------------------------------------------------------------------
# Each region's AppConfig holds a single value describing what THAT region is:
# "active" or "passive". The Lambda helper at lambda/shared/active_region.py
# reads it; schedulers short-circuit when their local region is "passive".
#
# Failover is operator-driven via the AppConfig console (NOT a terraform
# apply): the lifecycle ignore_changes on hosted_configuration_version.content
# prevents subsequent terraform applies from clobbering an operator flip.
#
# Chunk 02 adds the symmetric secondary-region copy under aws.secondary.
# ============================================================================

resource "aws_appconfig_application" "ha" {
  name        = "${var.environment}-${var.app_name}-ha"
  description = "Active/passive region flag for ${var.app_name}"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_environment" "ha" {
  application_id = aws_appconfig_application.ha.id
  name           = var.environment

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_configuration_profile" "ha_active_region" {
  application_id = aws_appconfig_application.ha.id
  name           = "active-region"
  location_uri   = "hosted"
  type           = "AWS.Freeform"

  tags = {
    Purpose = "ha-replication"
  }
}

# Instant deployment strategy: a failover flip must take effect immediately,
# not gradually. The whole point is failover speed.
resource "aws_appconfig_deployment_strategy" "instant" {
  name                           = "${var.environment}-${var.app_name}-ha-instant"
  deployment_duration_in_minutes = 0
  final_bake_time_in_minutes     = 0
  growth_factor                  = 100
  replicate_to                   = "NONE"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_hosted_configuration_version" "initial" {
  application_id           = aws_appconfig_application.ha.id
  configuration_profile_id = aws_appconfig_configuration_profile.ha_active_region.configuration_profile_id
  content_type             = "application/json"

  content = jsonencode({
    schema_version = 1
    role           = var.initial_region_role_primary
    activated_at   = timestamp()
  })

  lifecycle {
    # Subsequent terraform applies must not clobber an operator-driven flip.
    # Operators flip the value via the AWS AppConfig console (or
    # `aws appconfig create-hosted-configuration-version` + start-deployment)
    # during failover. timestamp() would also force a change on every apply
    # absent this ignore_changes; that's intentional — once initial deploy is
    # done, only humans touch this value.
    ignore_changes = [content]
  }
}

resource "aws_appconfig_deployment" "initial" {
  application_id           = aws_appconfig_application.ha.id
  environment_id           = aws_appconfig_environment.ha.environment_id
  deployment_strategy_id   = aws_appconfig_deployment_strategy.instant.id
  configuration_profile_id = aws_appconfig_configuration_profile.ha_active_region.configuration_profile_id
  configuration_version    = aws_appconfig_hosted_configuration_version.initial.version_number
  description              = "Initial active-region flag"

  tags = {
    Purpose = "ha-replication"
  }
}

# ============================================================================
# Secondary-region AppConfig copy (HA mode only).
# Each region's AppConfig is independent. Operator flips each one
# separately during failover via the AppConfig console.
# ============================================================================

resource "aws_appconfig_application" "ha_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "${var.environment}-${var.app_name}-ha"
  description = "Active/passive region flag for ${var.app_name} (secondary)"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_environment" "ha_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  application_id = aws_appconfig_application.ha_secondary[0].id
  name           = var.environment

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_configuration_profile" "ha_active_region_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  application_id = aws_appconfig_application.ha_secondary[0].id
  name           = "active-region"
  location_uri   = "hosted"
  type           = "AWS.Freeform"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_deployment_strategy" "instant_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                           = "${var.environment}-${var.app_name}-ha-instant"
  deployment_duration_in_minutes = 0
  final_bake_time_in_minutes     = 0
  growth_factor                  = 100
  replicate_to                   = "NONE"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_appconfig_hosted_configuration_version" "initial_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  application_id           = aws_appconfig_application.ha_secondary[0].id
  configuration_profile_id = aws_appconfig_configuration_profile.ha_active_region_secondary[0].configuration_profile_id
  content_type             = "application/json"

  content = jsonencode({
    schema_version = 1
    role           = var.initial_region_role_secondary
    activated_at   = timestamp()
  })

  lifecycle {
    ignore_changes = [content]
  }
}

resource "aws_appconfig_deployment" "initial_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  application_id           = aws_appconfig_application.ha_secondary[0].id
  environment_id           = aws_appconfig_environment.ha_secondary[0].environment_id
  deployment_strategy_id   = aws_appconfig_deployment_strategy.instant_secondary[0].id
  configuration_profile_id = aws_appconfig_configuration_profile.ha_active_region_secondary[0].configuration_profile_id
  configuration_version    = aws_appconfig_hosted_configuration_version.initial_secondary[0].version_number
  description              = "Initial active-region flag (secondary)"

  tags = {
    Purpose = "ha-replication"
  }
}

# Outputs consumed by Lambda environments (chunk 06 + chunk 07 wire these
# into the writer + scheduler env-var blocks).
output "appconfig_application_id" {
  description = "AppConfig application id for the HA role flag"
  value       = aws_appconfig_application.ha.id
}

output "appconfig_environment_id" {
  description = "AppConfig environment id for the HA role flag"
  value       = aws_appconfig_environment.ha.environment_id
}

output "appconfig_configuration_profile_id" {
  description = "AppConfig configuration-profile id for the HA role flag"
  value       = aws_appconfig_configuration_profile.ha_active_region.configuration_profile_id
}

output "appconfig_application_id_secondary" {
  description = "AppConfig application id in the secondary region (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_appconfig_application.ha_secondary[0].id : null
}

output "appconfig_environment_id_secondary" {
  description = "AppConfig environment id in the secondary region (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_appconfig_environment.ha_secondary[0].environment_id : null
}

output "appconfig_configuration_profile_id_secondary" {
  description = "AppConfig configuration-profile id in the secondary region (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_appconfig_configuration_profile.ha_active_region_secondary[0].configuration_profile_id : null
}
