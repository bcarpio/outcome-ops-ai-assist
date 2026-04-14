# AI Disclosure Modal

The AI Disclosure Modal is a pre-use notice that alerts users they are interacting with an AI system before any interaction occurs. It supports AI transparency and regulatory compliance requirements such as enterprise risk assessments and the EU AI Act. Users must acknowledge the disclosure before proceeding, and acceptance is stored locally so the modal only appears once per browser.

## Key Features

- Pre-use AI transparency notice displayed before any user interaction
- Covers key disclosures: AI-generated content may contain errors, interactions are logged, outputs are not professional advice
- One-time acceptance stored in browser localStorage
- Configurable via `VITE_AI_DISCLOSURE_ENABLED` environment variable (enabled by default)
- Supports enterprise AI risk assessment and regulatory compliance (EU AI Act)
- Can be disabled per environment for internal tooling use cases

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
