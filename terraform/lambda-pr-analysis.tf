# PR Analysis Lambdas
#
# Resources:
# - module "analyze_pr_lambda" (Analyze GitHub PRs and queue architecture check jobs)
# - module "process_pr_check_lambda" (Process PR check jobs from SQS, Claude-based)
# - aws_lambda_event_source_mapping "pr_checks_queue_to_lambda" (SQS trigger)
#
# Two-stage PR analysis: analyze-pr queues check jobs to SQS FIFO,
# process-pr-check runs Claude-based architectural and ADR compliance checks.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
