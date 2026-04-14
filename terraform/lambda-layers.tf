# Lambda Layers
#
# Resources:
# - null_resource "build_runtime_layer" (Build Git/Make/build tools layer)
# - module "runtime_tools_layer" (Lambda layer with Git, Make, build tools)
# - null_resource "build_terraform_layer" (Build Terraform CLI layer)
# - module "terraform_tools_layer" (Lambda layer with Terraform CLI)
#
# Runtime tools layer used by generate-code Lambda for git operations.
# Terraform tools layer used for .tf file formatting and validation.
# Run-tests Lambda uses a container image instead (see ecr.tf).
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
