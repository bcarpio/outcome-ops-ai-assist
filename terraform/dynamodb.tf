# DynamoDB Code Maps Table
#
# Resources:
# - module "code_maps_table" (DynamoDB table for state tracking)
# - aws_ssm_parameter "code_maps_table_name" (SSM parameter for table name)
# - aws_ssm_parameter "code_maps_table_arn" (SSM parameter for table ARN)
#
# Used for commit SHA tracking and incremental processing state.
# Vector embeddings stored in S3 Vectors (not DynamoDB).
# DynamoDB Streams enabled for change data capture.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
