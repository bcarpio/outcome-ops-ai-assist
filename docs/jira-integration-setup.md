# Jira Integration Setup

This guide walks you through setting up Jira Automation to trigger code generation from Jira issues.

## Overview

The Jira integration allows you to:
- Add `approved-for-plan` label to generate an implementation plan
- Add `approved-for-generation` label to generate full code + tests + PR

**Flow:** Jira Issue → Jira Automation Rule → AWS SSM Document → Lambda → GitHub PR

## Prerequisites

Before starting, ensure you have:
- OutcomeOps AI Assist deployed with `enable_jira_integration = true`
  (post-ADR-040 this flag ALSO provisions the workspace-level Jira
  Lambdas that ingest Jira issues as chat context. If you only want
  the Automation flow described in this runbook and not workspace-
  level Jira ingest, note that today they ship together.)
- Admin access to your Jira project
- AWS CLI configured with appropriate permissions

---

## Step 1: Create Labels in Jira (Workaround Required)

Jira Automation can only select labels that already exist on at least one issue. Create a "Label Init" story to establish the required labels.

1. Create a new Jira issue titled "Label Init - Do Not Delete"
2. Add both labels to this issue:
   - `approved-for-plan`
   - `approved-for-generation`
3. Move this issue to the backlog (never assign or delete it)

> **Why?** Jira Automation's label condition dropdown only shows labels that exist on at least one issue in the project.

---

## Step 2: Create Components for Each Repository

Each repository you want to enable for code generation needs a Jira component with a specific naming convention.

1. Go to **Project Settings** → **Components**
2. Create a component with the exact GitHub repository path:
   - Name: `owner/repo` (e.g., `bcarpio/outcome-ops-ai-assist`)
   - Description: Optional

> **Important:** The component name MUST match the GitHub repository path exactly (case-sensitive).

When creating issues, assign them to the appropriate component to specify which repo should receive the generated code.

---

## Step 3: Deploy Terraform (Phase 1)

Deploy the infrastructure to create the IAM role:

```bash
cd terraform

# Enable Jira integration
# In your tfvars file, set:
# enable_jira_integration = true

terraform plan -var-file=prd.tfvars
terraform apply -var-file=prd.tfvars
```

After successful deployment, copy the IAM role ARN from the output:

```
jira_automation_role_arn = "arn:aws:iam::123456789012:role/atlassian-automation-prd-outcome-ops-ai-assist"
```

---

## Step 4: Configure Jira AWS Connector

1. Go to **Jira Settings** → **Automation** → **Library** → **Connect to AWS**
2. Click **Add AWS account**
3. Enter the Role ARN from Step 3
4. Jira will display an **External ID** - copy this value

Example External ID:
```
automation-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Step 5: Deploy Terraform (Phase 2)

Update your tfvars with the External ID and redeploy:

```hcl
# prd.tfvars
enable_jira_integration = true
jira_external_id        = "automation-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

```bash
terraform apply -var-file=prd.tfvars
```

This updates the IAM role trust policy with the correct External ID.

---

## Step 6: Create Jira Automation Rule

1. Go to **Project Settings** → **Automation** → **Create rule**

### Trigger Configuration

**When:** Work Item Transitioned
- From status: `To Do`
- To status: `In Progress`

### Condition Configuration

**If:** Labels contain any of
- `approved-for-plan`
- `approved-for-generation`

### Action Configuration

**Then:** Run AWS Systems Manager automation

Configure the SSM action:
- **AWS Account:** Select your connected AWS account
- **Region:** Your deployment region (e.g., `us-west-2`)
- **Document:** `prd-outcome-ops-ai-assist-jira-automation`

**Parameters:**

| Parameter | Smart Value |
|-----------|-------------|
| `issueKey` | `{{issue.key}}` |
| `issueSummary` | `{{issue.summary}}` |
| `issueDescription` | `{{issue.description}}` |
| `componentKey` | `{{issue.components.name}}` |
| `label` | `{{trigger.label.name}}` |

> **Note:** The `label` parameter captures which label triggered the automation (`approved-for-plan` or `approved-for-generation`).

### Save and Enable

1. Name your rule (e.g., "OutcomeOps Code Generation")
2. Save the rule
3. Enable it

---

## Step 7: Test the Integration

1. Create a new Jira issue with:
   - A descriptive title (becomes the GitHub issue title)
   - Detailed description (user story, acceptance criteria)
   - Component set to your target repo (e.g., `bcarpio/my-repo`)

2. Add the `approved-for-plan` label and move to "In Progress"

3. Check the automation audit log for success/failure

4. Verify a PR was created in GitHub with the implementation plan

5. To generate full code, add `approved-for-generation` label

---

## Troubleshooting

### "Parameter not found: GetParameter"

Your Jira issue description contains `{{variable}}` patterns that Jira interprets as smart values. Use `{code}` blocks or escape the braces.

### Automation rule doesn't trigger

- Verify the issue has the correct label
- Verify the issue transitioned from "To Do" to "In Progress"
- Check that the component is set on the issue

### SSM Document not found

- Verify the document name matches: `{env}-{app_name}-jira-automation`
- Verify you selected the correct AWS region

### Access Denied errors

- Verify the External ID in tfvars matches Jira's External ID exactly
- Redeploy Terraform after updating the External ID
- Check IAM role trust policy in AWS Console

---

## Architecture Reference

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Jira Issue    │     │  Jira Automation│     │   AWS SSM       │
│                 │────▶│      Rule       │────▶│   Document      │
│ + Label added   │     │ (Work Item      │     │                 │
│ + Component set │     │  Transitioned)  │     │                 │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   GitHub PR     │◀────│  generate-code  │◀────│  SQS Queue      │
│                 │     │     Lambda      │     │  (FIFO)         │
│ + Plan/Code     │     │                 │     │                 │
│ + Tests         │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

## Labels Reference

| Label | Behavior |
|-------|----------|
| `approved-for-plan` | Creates PR with implementation plan only (no code) |
| `approved-for-generation` | Creates PR with full code, tests, and documentation |

You can use plan-only first to review the approach, then add generation label to proceed.
