# MCP Server Base Module - Variables
#
# Variables:
# - environment (string, required)
# - app_name (string, required)
# - aws_region (string, required)
# - server_name (string, required)
# - container_image (string, required)
# - container_port (number, default 8080)
# - cpu (number, default 512)
# - memory (number, default 1024)
# - environment_variables (map(string), default {})
# - secrets (map(string), default {})
# - ecs_cluster_arn (string, required)
# - vpc_id (string, required)
# - private_subnet_ids (list(string), required)
# - assign_public_ip (bool, default false)
# - service_discovery_namespace_id (string, required)
# - service_discovery_namespace_name (string, required)
# - alb_listener_arn (string, default "")
# - alb_listener_rule_priority (number, default 100)
# - mcp_path_pattern (string, default "/mcp")
# - health_check_matcher (string, default "200-499")
# - allowed_security_group_ids (list(string), default [])
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
