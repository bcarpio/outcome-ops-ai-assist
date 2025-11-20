# Context Engineering – Open Framework

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=bcarpio_outcome-ops-ai-assist&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=bcarpio_outcome-ops-ai-assist)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=bcarpio_outcome-ops-ai-assist&metric=coverage)](https://sonarcloud.io/summary/new_code?id=bcarpio_outcome-ops-ai-assist)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=bcarpio_outcome-ops-ai-assist&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=bcarpio_outcome-ops-ai-assist)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=bcarpio_outcome-ops-ai-assist&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=bcarpio_outcome-ops-ai-assist)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=bcarpio_outcome-ops-ai-assist&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=bcarpio_outcome-ops-ai-assist)
[![CI/CD](https://github.com/bcarpio/outcome-ops-ai-assist/actions/workflows/cicd.yml/badge.svg)](https://github.com/bcarpio/outcome-ops-ai-assist/actions/workflows/cicd.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

This repository contains the **open portion** of Context Engineering: ADR templates, methodology, documentation standards, and educational examples.

The full autonomous agent platform (issue → code generation → PR → test execution → self-correction loop, air-gapped deployment, integration with internal LLMs) is proprietary, battle-tested at Fortune 10 scale in regulated environments, and only available via advisory engagements or licensed deployments.

**For enterprise briefings, transformation engagements, or licensing:** https://www.linkedin.com/in/briancarpio/

---

## The Enterprise Problem

**Generic AI generates generic code. Regulated industries need code that matches YOUR standards.**

Fortune 500 engineering organizations face a common challenge:
- AI tools generate code that violates internal security policies
- Generic implementations don't match architectural decisions
- Teams spend hours adapting AI output to organizational patterns
- Compliance requirements prohibit sending code to external LLMs
- **$200K/yr GitHub Copilot seats still require extensive human rework**

**The Context Engineering solution:**
- AI trained on YOUR ADRs, YOUR patterns, YOUR standards
- Runs on YOUR infrastructure (air-gapped, internal LLMs)
- Zero IP exfiltration, full compliance with regulatory requirements
- **10-15x velocity improvement in regulated codebases**

---

## What's Open Source vs. Proprietary

### Open Source (This Repository)

✅ **ADR Methodology**
- [ADR Template](docs/adr/TEMPLATE.md)
- [Getting Started with ADRs](docs/getting-started-with-adrs.md)
- Example ADRs demonstrating best practices

✅ **Context Engineering Philosophy**
- [Backstory](docs/backstory.md) - Why context engineering matters
- [High-level Architecture](docs/architecture.md) - Conceptual design
- Methodology and approach

✅ **Educational Resources**
- How to structure organizational knowledge
- Best practices for AI-assisted development
- Community learning materials

### Proprietary Enterprise Platform

🔒 **Autonomous Agent System**
- Issue → Code Generation → PR → Test → Self-Correction Loop
- Prompt engineering and tool-calling chains
- Multi-agent orchestration architecture

🔒 **Production Infrastructure**
- Air-gapped deployment for regulated environments
- Integration with internal LLMs (Bedrock, Azure OpenAI, on-prem)
- Zero IP exfiltration architecture

🔒 **Enterprise Features**
- SSO/SAML integration
- Compliance reporting and audit trails
- Custom model fine-tuning on organizational code
- 24/7 SLA support

**Available only via:**
- 6-24 month transformation engagements
- Licensed deployments for Fortune 500
- Advisory services for platform engineering teams

[Request Enterprise Briefing →](docs/enterprise-briefing.md)

---

## Proven at Scale

**Fortune 10 Pharmaceutical Company:**
- 87% reduction in feature delivery time
- Zero IP exfiltration (air-gapped deployment)
- Full HIPAA/SOX compliance
- Engineering velocity from 2-week sprints to same-day delivery

**Consumer Digital Platform (>100M users):**
- 16-hour tasks → 15 minutes
- $0.68 per feature (vs. $15K manual development)
- 100-200x ROI
- Production-tested, self-correcting code generation

**All deployments:** Air-gapped, internal LLMs, zero external API calls, full audit trails.

---

## The Context Engineering Methodology

**Phase 1: Capture Organizational Knowledge (Weeks 1-4)**
- Document architectural decisions as ADRs
- Generate code-maps of existing patterns
- Codify testing, security, and compliance standards
- Build searchable knowledge base

**Phase 2: Deploy Agent Platform (Weeks 5-8)**
- Air-gapped infrastructure on your AWS/Azure
- Integration with internal LLM endpoints
- GitHub/GitLab/Bitbucket integration
- SSO and access controls

**Phase 3: Pilot & Validation (Weeks 9-16)**
- Select pilot team and feature backlog
- Monitor generation quality and velocity
- Refine ADRs based on agent output
- Measure time-to-PR and approval rates

**Phase 4: Scale & Optimize (Weeks 17-24)**
- Roll out to additional teams
- Custom model fine-tuning (optional)
- Advanced self-correction workflows
- Executive metrics and reporting

**Result:** 10-15x velocity improvement, full compliance, zero vendor lock-in.

---

## Open Framework: Getting Started with ADRs

The foundation of Context Engineering is capturing organizational knowledge in a machine-readable format. **Architecture Decision Records (ADRs)** are the key.

**New to ADRs?** [Start here →](docs/getting-started-with-adrs.md)

**Example ADRs in this repository:**
- [ADR-001: Creating ADRs](docs/adr/ADR-001-create-adrs.md)
- [ADR-006: Python Testing Import Patterns](docs/adr/ADR-006-python-testing-imports.md)
- [ADR-007: Documentation-Driven Decision Making](docs/adr/ADR-007-documentation-driven-decisions.md)

**Using the ADR template:**
```bash
# Copy template for new decision
cp docs/adr/TEMPLATE.md docs/adr/ADR-XXX-your-decision.md

# Document the decision
# - Context: Why this decision is needed
# - Decision: What you decided and why
# - Consequences: Impact and tradeoffs
```

**ADRs enable:**
- AI understanding of organizational patterns
- Consistent code generation across teams
- Knowledge transfer for new engineers
- Audit trails for compliance

---

## Documentation

**Open Source Resources**
- [Getting Started with ADRs](docs/getting-started-with-adrs.md)
- [ADR Template](docs/adr/TEMPLATE.md)
- [Example ADRs](docs/adr/)
- [Backstory: Why Context Engineering](docs/backstory.md)
- [High-Level Architecture](docs/architecture.md)

**Enterprise Platform** (proprietary)
- Lambda function implementations
- Prompt engineering and agent chains
- Multi-agent orchestration
- Self-correction algorithms
- Deployment automation

[Request Enterprise Documentation →](docs/enterprise-briefing.md)

---

## Who Built This

**Brian Carpio** – Former AWS ProServe Principal, Platform Engineering Leader

**Track record:**
- **AWS ProServe (2017-2020):** Led largest Healthcare & Life Sciences engagement in ProServe history
- **Golden Pipelines (2014):** 6 weeks → 1 week deployment cycles at Aetna
- **Platform Engineering Pioneer:** Built self-service infrastructure before it had a name (Pearson 2012, Aetna 2014, Comcast 2018)

**Context Engineering** applies the same playbook to AI-assisted development:
- Make the easy path the right path
- Bake standards into infrastructure
- Create velocity through automation
- **Golden pipelines for code generation**

[Read the full story →](docs/backstory.md)

---

## Get Started

**Learn the Methodology (Free):**
- [Getting Started with ADRs](docs/getting-started-with-adrs.md)
- [Backstory: Why Context Engineering](docs/backstory.md)
- [Example ADRs](docs/adr/)
- [Join Community Discussions](https://github.com/bcarpio/outcome-ops-ai-assist/discussions)

**Enterprise Transformation:**
- [Request 30-Minute Briefing](docs/enterprise-briefing.md)
- [Review Enterprise Platform Overview](docs/enterprise-briefing.md)
- [Connect on LinkedIn](https://www.linkedin.com/in/briancarpio/)

**Contributing to Open Framework:**
- Found a typo in ADR docs? [Open an issue](https://github.com/bcarpio/outcome-ops-ai-assist/issues)
- Have an ADR example to share? [Start a discussion](https://github.com/bcarpio/outcome-ops-ai-assist/discussions)
- Want to improve templates? [Submit a PR](https://github.com/bcarpio/outcome-ops-ai-assist/pulls)

---

**Built for engineering velocity.**

Outcome-driven development. Infrastructure by code. Engineering outcomes by AI. Your vision, automated execution.

**Context Engineering:** The only framework purpose-built for AI-assisted development in regulated industries.

For Fortune 500 transformation engagements: https://www.linkedin.com/in/briancarpio/
