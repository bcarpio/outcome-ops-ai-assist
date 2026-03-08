# Terraform Lambda Layer

This layer provides the Terraform CLI for Lambda functions that need to format or validate Terraform files.

**Note:** Layer binaries are not committed to git. You must build the layer before running `terraform apply`.

## Contents

- `/opt/bin/terraform` - Terraform CLI v1.9.5

## Usage

Add this layer to your Lambda function, and terraform will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Build Layer

```bash
./scripts/build-terraform-layer.sh
```

Then run `terraform apply` to update the layer version.
