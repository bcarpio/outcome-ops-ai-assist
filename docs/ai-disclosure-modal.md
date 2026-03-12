# AI Disclosure Modal

A pre-use notice that alerts users they are interacting with an AI system
before any interaction occurs. This supports AI transparency and regulatory
compliance requirements (e.g., enterprise risk assessments, EU AI Act).

## What It Does

On first visit, a modal appears informing the user that:

- They are interacting with an AI system, not a human
- AI-generated content may contain errors and should be reviewed
- They should not share sensitive personal information beyond what is required
- Interactions are logged for audit and security purposes
- AI outputs do not constitute professional advice

The user must click "I Understand" to proceed. Acceptance is stored in
`localStorage` (`ai-disclosure-accepted`) so the modal only appears once per
browser.

## Configuration

The modal is controlled by the `VITE_AI_DISCLOSURE_ENABLED` environment
variable. It defaults to **enabled** if the variable is not set.

| Value     | Behavior              |
|-----------|-----------------------|
| `true`    | Modal is shown        |
| `false`   | Modal is disabled     |
| (not set) | Modal is shown        |

### Environment Files

```
ui/.env.example       # Committed - reference template
ui/.env.development   # Git-ignored - local dev settings
ui/.env.production    # Git-ignored - production build settings
```

To disable the modal for a specific environment:

```bash
# In the appropriate .env file
VITE_AI_DISCLOSURE_ENABLED=false
```

### Build-Time Override

Since Vite inlines `VITE_*` variables at build time, you can also set it
during the build:

```bash
VITE_AI_DISCLOSURE_ENABLED=false npm run build
```

## Files

| File | Purpose |
|------|---------|
| `ui/src/components/AiDisclosureModal.tsx` | Modal component |
| `ui/src/main.tsx` | Renders modal at app root |
| `ui/.env.example` | Environment variable template |
| `ui/.env.development` | Dev environment config (git-ignored) |
| `ui/.env.production` | Production environment config (git-ignored) |

## Resetting Acceptance

To re-show the modal (useful for testing), clear the localStorage key:

```javascript
localStorage.removeItem('ai-disclosure-accepted')
```

## Risk Assessment Coverage

This feature addresses the following AI transparency controls:

- **AI pre-use notice(s) prior to user AI interaction** - The modal appears
  before any interaction is possible
- **Alerting individuals they are interacting with an AI system and not a
  human** - The modal explicitly states the user is interacting with AI, not
  a human
