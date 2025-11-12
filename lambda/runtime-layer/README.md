# Runtime Tools Lambda Layer

This layer provides git, make, terraform, and related build tools for the run-tests Lambda.

## Contents

- `/opt/bin/git` - Git version control
- `/opt/bin/make` - GNU Make
- `/opt/bin/terraform` - Terraform CLI
- `/opt/bin/tar`, `/opt/bin/gzip`, `/opt/bin/unzip` - Archive tools
- `/opt/lib/` - Shared library dependencies

## Usage

Add this layer to your Lambda function, and the tools will be available in `/opt/bin/`.
The Lambda execution environment automatically adds `/opt/bin` to the PATH.

## Rebuild

To rebuild this layer:
```bash
./scripts/build-runtime-layer.sh
```

Then run `terraform apply` to update the layer version.
