# Code Generation Lambdas
#
# Resources:
# - module "generate_code_lambda" (Process GitHub webhooks for code generation)
# - aws_lambda_event_source_mapping "code_generation_queue_to_lambda" (SQS trigger)
# - module "generate_code_dlq_lambda" (Handle failed code generation from DLQ)
# - aws_lambda_event_source_mapping "code_generation_dlq_to_lambda" (DLQ trigger)
# - module "run_tests_lambda" (Container-based multi-language test runner)
# - aws_cloudwatch_event_rule "code_generation_completed" (EventBridge trigger)
# - aws_cloudwatch_event_target "run_tests_target" (EventBridge target)
# - aws_lambda_permission "allow_eventbridge_run_tests" (EventBridge permission)
# - module "handle_command_lambda" (Process PR comment commands)
# - data "aws_ssm_parameter" "run_tests_image_tag" (Container image tag)
# - local "run_tests_policy_statements" (Shared IAM policies for run-tests)
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
