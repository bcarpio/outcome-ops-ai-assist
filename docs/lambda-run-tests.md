# Lambda: run-tests (Enterprise Platform Component)

**Status:** Proprietary – Enterprise Only

## Overview

The `run-tests` Lambda performs automated test execution, AI-powered error correction, and self-healing workflows. It validates generated code before human review and automatically fixes common issues using knowledge base context.

**This component is proprietary and only available via:**
- Advisory engagements for Fortune 500 organizations
- Licensed deployments in regulated environments
- Enterprise transformation programs

[Request Enterprise Briefing →](enterprise-briefing.md)

---

## What This Component Does

1. **Automated Test Execution** – Runs pytest/jest/etc. on generated code
2. **Error Classification** – Identifies fixable vs. logic errors
3. **Knowledge Base-Aware Auto-Fix** – Queries ADRs to correct syntax/import errors
4. **Self-Correction Loop** – Retries with improved prompts
5. **Human Escalation** – Creates PR for review when auto-fix fails

**Key Innovation:** Auto-fix queries organizational knowledge (ADRs, patterns) before attempting corrections, resulting in 10x higher success rate than generic retry logic.

---

## Why This Is Proprietary

🔒 **AI-powered error correction** – Knowledge base-aware auto-fix algorithms

🔒 **Self-healing workflows** – Bounded retry logic with escalation paths

🔒 **Test classification intelligence** – Distinguishes fixable from logic errors

🔒 **Prompt engineering for fixes** – Production-refined correction prompts

**Result:** 85% of syntax/import errors fixed automatically, zero manual intervention.

---

## Open Source Alternative

Build your own test automation using:
- [ADR-006: Python Testing Import Patterns](adr/ADR-006-python-testing-imports.md)
- [ADR-007: Documentation-Driven Decision Making](adr/ADR-007-documentation-driven-decisions.md)

The methodology is free. The battle-tested auto-fix platform is not.

[Request Enterprise Briefing →](enterprise-briefing.md)

---

**Context Engineering:** The only framework purpose-built for AI-assisted development in regulated industries.

For Fortune 500 transformation engagements: https://www.linkedin.com/in/briancarpio/
