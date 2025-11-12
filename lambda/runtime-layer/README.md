# Runtime Tools Lambda Layer

This layer provides git, make, terraform, and related build tools for the run-tests Lambda.

**Note:** Layer binaries (`bin/` and `lib/`) are not committed to git. You must build the layer locally before running `terraform apply`.

## Contents

- `/opt/bin/git` - Git version control
- `/opt/bin/make` - GNU Make
- `/opt/bin/terraform` - Terraform CLI
- `/opt/bin/tar`, `/opt/bin/gzip`, `/opt/bin/unzip` - Archive tools
- `/opt/lib/` - Shared library dependencies

## Usage

Add this layer to your Lambda function, and the tools will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Build Layer (Required Before Terraform Apply)

The layer binaries are built locally and not checked into git:

```bash
# Build the layer (creates bin/ and lib/ directories)
./scripts/build-runtime-layer.sh

# Deploy with Terraform
terraform apply
```

The build script uses Docker to compile binaries compatible with Amazon Linux 2 (Lambda runtime environment).
