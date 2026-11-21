# Contributing to OutcomeOps AI Assist

Thanks for your interest in contributing. OutcomeOps AI Assist is a generic,
language-agnostic platform for AI-powered code generation, PR review, and knowledge-base
retrieval that deploys into the customer's own AWS account. This guide covers how to get
set up and what we expect in a pull request.

## License and Developer Certificate of Origin

This project is licensed under the **Apache License, Version 2.0** (see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE)). By contributing, you agree that your contributions are licensed
under the same terms.

We use the **Developer Certificate of Origin (DCO)**. Sign off every commit to certify you
wrote the patch or otherwise have the right to submit it under the project's license:

```bash
git commit -s -m "feat(scope): short description"
```

The `-s` flag adds a `Signed-off-by: Your Name <you@example.com>` trailer. Commits without
a sign-off will be asked to amend.

Every new first-party source file must carry the canonical per-file SPDX header defined in
[`docs/adr/ADR-008-license-compliance.md`](docs/adr/ADR-008-license-compliance.md):

```python
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
```

(Use the `//` or `/* */` comment form for JS/TS/CSS — see the ADR for the exact text per
language.)

## Getting set up

Prerequisites: Python 3.12, Node 20+ (for the UI), Terraform 1.5+, and Docker.

```bash
# Create the Python virtualenv and install every lambda/*/requirements.txt
make setup
source venv/bin/activate
```

See [`docs/getting-started.md`](docs/getting-started.md) for the full prerequisite list and
a first deployment walkthrough.

## Before you open a pull request

Run the same checks CI runs. Only the ones relevant to what you changed are required, but
all are fast:

```bash
make test-unit      # Lambda/Python unit tests (always)
make test-ui        # React/Vitest tests (if you touched ui/; needs Node 20+)
make fmt            # terraform fmt -recursive (if you touched .tf)
make validate-tf    # terraform validate (if you touched .tf)
make audit          # npm audit + pip-audit dependency scan
```

`make all` runs format, validate, and tests together. If `make audit-py` hangs or every
file reports `FAIL:`, point `TMPDIR` off `/tmp` and re-run — pip-audit builds an isolated
venv per requirements file and some hardened hosts block execution from `/tmp`:

```bash
mkdir -p ~/.cache/py-audit-tmp && TMPDIR=$HOME/.cache/py-audit-tmp make audit-py
```

## Automated review on your PR

This project **runs on its own product.** When you open a pull request, the OutcomeOps PR
Analyzer — the very platform in this repository — reviews your change automatically via
[`.github/workflows/pr-analysis.yml`](.github/workflows/pr-analysis.yml) and posts its
findings as PR comments. Every check is grounded in this repo's own ADRs and standards, so
the feedback reflects our conventions rather than generic lint. The checks:

- **ADR compliance** — does the change follow the decisions recorded in [`docs/adr/`](docs/adr/)?
- **Breaking changes** — would it break a downstream consumer of a changed symbol?
- **Architectural duplication** — does it re-implement logic that already exists?
- **Test coverage** — are new code paths covered by tests?
- **README freshness** — do docs describing the changed code need updating?
- **License compliance** — no GPL/AGPL/SSPL dependencies, and every new source file carries
  the SPDX header required above (the ADR-008 policy the analyzer enforces).

Please read the analyzer's comments and **resolve anything it flags (WARN or FAIL) before
asking for human review** — or reply explaining why a finding doesn't apply. Treat it as a
first reviewer that already knows the codebase's standards.

> The analyzer assumes an AWS role via OIDC, so it runs automatically on PRs from branches
> in this repository. GitHub does not expose repository secrets (including that role) to
> workflows triggered from a **fork**, so for a fork PR a maintainer re-runs the analysis
> once they've had a first look at your change.

## Commit and PR conventions

- Use [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`
  (e.g. `fix(router): degrade to 'pattern' on classify failure`).
- Keep commits focused; write a clear body explaining the "why."
- No emojis in commit messages or documentation.
- Sign off every commit (DCO, above).
- Architectural decisions belong in an ADR under [`docs/adr/`](docs/adr/), not just in the
  commit. See [`docs/adr/ADR-001-create-adrs.md`](docs/adr/ADR-001-create-adrs.md).

## Project layout

```
lambda/           # Lambda handlers (one dir per function)
lambda/tests/     # Centralized pytest tests
terraform/        # Infrastructure as Code
docs/             # Documentation and ADRs
scripts/          # CLI tools and operator tooling
ui/               # React frontend + Express proxy server
```

## Documentation

- [Getting Started](docs/getting-started.md)
- [Architecture Overview](docs/architecture.md)
- [Architecture Decision Records](docs/adr/)

Questions or a change you're unsure about? Open an issue to discuss before investing in a
large PR.

## Backlog and issues

The backlog is the repository's [GitHub Issues](https://github.com/bcarpio/outcome-ops-ai-assist/issues)
(ADR-062). To propose work, open an issue with the **Backlog item** template and add one type
label (`bug`, `enhancement`, `tech-debt`, `documentation`), one priority label
(`priority: high|medium|low`) and at least one area label (`area: pr-checks`, `area: ingestion`,
`area: symbol-graph`, `area: models`, `area: terraform`, `area: integrations`, `area: dx`).
`good first issue` marks small, self-contained items. Title issues `<component>: <what should
change>` (for example `BREAKING_CHANGES: compare signatures for Go functions`); name branches
`<type>/<short-slug>`.

Never add the `approved-for-generation` label to a backlog issue: it hands the issue to the
code-generation pipeline. Reference the issue from your PR with `Closes #N`.
