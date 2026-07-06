# ADR-041: Container image build hardening

## Status: Accepted

## Context

Four platform components ship as container images rather than Lambda
zips: `run-tests`, `mcp-server` (Fargate), `database-integration`, and
`database-sync`. A SonarCloud security review (2026-07-06) flagged the
same weaknesses across all four Dockerfiles:

- A BLOCKER: the run-tests image bootstrapped Node.js by piping an
  unverified downloaded script into bash (`curl ... | bash`), executing
  whatever the remote host served at build time (docker:S8482).
- Downloaded artifacts (git source tarball, Terraform zip) were
  extracted and executed with no checksum verification.
- Python dependencies installed into images used `>=` version floors,
  so two builds of the "same" image could contain different dependency
  trees (docker:S8543/S8544).
- `pip install` permitted sdists, meaning a package's `setup.py` could
  execute arbitrary code during the image build (docker:S8541).
- Images ran as root by default (docker:S6471 hotspots).

The target customers are regulated enterprises whose container-scanning
pipelines flag exactly these patterns; images that fail a customer's
admission scan block the whole deployment.

ADR-034 governs Python dependency management for **zip-packaged
Lambdas** and intentionally uses `>=` floors there. It does not address
image builds, where reproducibility is the dominant concern.

## Decision

All Dockerfiles in this repository MUST follow these rules:

### 1. Never pipe downloaded scripts into a shell

Prefer distro packages (GPG-verified by dnf/apt) over vendor bootstrap
scripts. Example: Node.js 20 comes from the Amazon Linux 2023
`nodejs20` + `nodejs20-npm` packages, not from the NodeSource setup
script.

### 2. Checksum-verify every downloaded artifact

Any `curl`/`ADD` of a build input must be followed by a `sha256sum -c`
against a hash pinned in the Dockerfile (as an `ARG` next to the
version `ARG`, bumped together). Fetch from the canonical source that
publishes checksums (kernel.org for git, releases.hashicorp.com for
Terraform).

Exception: the AWS RDS global CA bundle
(`truststore.pki.rds.amazonaws.com`) rotates by design and has no
stable hash; it is fetched over TLS from an AWS-operated host and left
unpinned deliberately.

### 3. Exact-pin Python dependencies installed into images

Requirements files consumed by a `RUN pip install` inside a Dockerfile
(`lambda/run-tests/requirements.txt`, `lambda/mcp-server/requirements.txt`,
`lambda/db-introspection-requirements.txt`) use `==` pins for every
entry. This scopes to container images only — zip-Lambda requirements
keep the ADR-034 `>=` floor policy, because their resolved versions are
controlled by the layer build, not by an unattended image rebuild.

### 4. Wheels only

Image `pip install` commands pass `--only-binary=:all:` so pip can
never execute a package's `setup.py` at build time. If a future
dependency has no wheel, build it in a separate builder stage and COPY
the result — do not drop the flag globally.

### 5. Non-root USER

Images end with a non-root `USER` directive. For Lambda container
images a numeric UID (e.g. `USER 1000`) with `HOME=/tmp` is sufficient;
`/tmp` is the only writable path at runtime anyway. The Fargate
mcp-server image predates this rule and should adopt it on its next
substantive change.

## Consequences

- Image builds are reproducible: the same Dockerfile at the same commit
  produces the same dependency tree.
- Version bumps are deliberate, visible in diffs, and testable —
  nothing upgrades because a rebuild happened to run on a later date.
- Bumping a pinned tool version now requires updating its SHA256
  alongside (the build fails loudly on mismatch, which is the point).
- Generated Dockerfiles (codegen) can be checked against these rules
  via the KB once this ADR is ingested.

## Version History

- v1.0 (2026-07-06): Initial version, prompted by SonarCloud security
  review (1 blocker, 9 major findings, 2 root-user hotspots across the
  four container images).
