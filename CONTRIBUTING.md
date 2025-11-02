# Contributing to OutcomeOps AI Assist

Thank you for your interest in OutcomeOps AI Assist! This guide explains how to contribute to this project and how to adapt it for your own organization.

## About This Project

OutcomeOps AI Assist is a framework for AI-assisted development. It's designed to be **forked and adapted** for your own organization's patterns and ADRs.

## Contributing to the Framework

If you have improvements to the **core framework** (Terraform patterns, Lambda standards, testing approach, development workflow), we'd love your contributions!

### How to Contribute

1. **Fork the repository**
   ```bash
   git clone git@github.com:your-org/outcome-ops-ai-assist.git
   cd outcome-ops-ai-assist
   ```

2. **Create a feature branch**
   ```bash
   git checkout -b feature/your-improvement
   ```

3. **Make your changes**
   - Update relevant ADRs if decision-making changes
   - Follow existing code patterns and conventions
   - Test your changes locally

4. **Submit a pull request**
   - Describe what problem your change solves
   - Reference relevant ADRs
   - Explain tradeoffs if applicable

### What We Accept

- **Framework improvements**: Better Terraform patterns, testing approaches, development workflows
- **Bug fixes**: Issues in Lambda handlers, Terraform modules, or documentation
- **Documentation**: Clearer explanations, better examples, new patterns
- **ADR enhancements**: Refined decision records that benefit all users

### What We Don't Accept

- Organization-specific implementations (use your own fork for that)
- Proprietary tools or paid dependencies
- Breaking changes without discussion

## Forking for Your Organization

This is the primary use case! Here's how to adapt OutcomeOps for your own project:

### 1. Fork the Repository

```bash
git clone git@github.com:your-org/outcome-ops-ai-assist.git
cd outcome-ops-ai-assist
```

### 2. Create Your Organization's ADRs Repository

Create a separate repository for your organization's ADRs:

```bash
# Example: your-org-adrs
mkdir ../your-org-adrs
cd ../your-org-adrs
git init
git add .
git commit -m "docs(adr): initial commit with organization ADRs"
git remote add origin git@github.com:your-org/your-org-adrs.git
git push -u origin main
```

### 3. Replace Example Content

Update the example files to match your organization:

**terraform/dev.tfvars.example:**
```hcl
environment = "dev"
repos_to_ingest = [
  {
    name    = "outcome-ops-ai-assist"
    project = "your-org/outcome-ops-ai-assist"
    type    = "standards"
  },
  {
    name    = "your-org-adrs"
    project = "your-org/your-org-adrs"
    type    = "standards"
  },
  {
    name    = "your-main-app"
    project = "your-org/your-main-app"
    type    = "application"
  }
]
```

**Create your real tfvars files** (which stay local):
```bash
cp terraform/dev.tfvars.example terraform/dev.tfvars
cp terraform/prd.tfvars.example terraform/prd.tfvars
# Edit with your actual configuration and repos
```

### 4. Create Your ADRs

In your `your-org-adrs` repository, create ADRs for your domain:

**your-org-adrs/docs/adr/ADR-001-domain-model.md:**
```markdown
# ADR-001: Domain Model

## Status: Accepted

## Context
Our organization builds [what you build].

## Decision
Our core entities are:
- [Entity 1]: [description]
- [Entity 2]: [description]

## Implementation
[Your patterns and examples]
```

### 5. Deploy

```bash
cd terraform
terraform init
terraform workspace new dev
terraform plan -var-file=dev.tfvars -out=terraform.dev.out
terraform apply terraform.dev.out
```

## Development Workflow

When working on your fork:

1. **Make changes locally**
2. **Run checks**: `make fmt lint test`
3. **Commit with conventional commits**: `git commit -m "feat(component): description"`
4. **Push to your fork**
5. **Deploy infrastructure**: `terraform apply`

See [ADR-002](docs/adr/ADR-001-create-adrs.md) for detailed workflow.

## Questions?

- Check [ADR-001](docs/adr/ADR-001-create-adrs.md) for creating your own ADRs
- Read [README.md](README.md) for architecture overview
- Open an issue if you find bugs in the framework

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) file for details.

You're free to fork, modify, and use this framework for your organization's AI-assisted development workflow.

---

**Happy building!** 🚀

Your fork becomes the knowledge base for your organization's development practices. The more patterns you document, the better Claude can generate code that matches your exact conventions.
