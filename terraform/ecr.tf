# ECR Repositories
#
# Resources:
# - aws_ecr_repository "run_tests" (Container image repo for run-tests Lambda)
# - aws_ecr_lifecycle_policy "run_tests" (Keep last 5 images)
# - aws_ecr_repository "chat_ui" (Container image repo for chat UI, conditional on deploy_ui)
# - aws_ecr_lifecycle_policy "chat_ui" (Keep last 10 images)
#
# Immutable tags for run-tests, mutable for chat-ui.
# Image scanning on push, optional KMS encryption.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
