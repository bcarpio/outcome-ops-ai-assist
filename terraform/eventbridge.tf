resource "aws_cloudwatch_event_bus" "automation" {
  name = "${var.environment}-${var.app_name}-bus"

  tags = {
    Purpose = "automation-events"
  }
}

output "automation_event_bus_name" {
  description = "Name of the automation EventBridge bus"
  value       = aws_cloudwatch_event_bus.automation.name
}

output "automation_event_bus_arn" {
  description = "ARN of the automation EventBridge bus"
  value       = aws_cloudwatch_event_bus.automation.arn
}
