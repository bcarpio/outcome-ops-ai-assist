# Lambda: Run Tests

The run-tests Lambda provides automated test validation for AI-generated code branches. It consumes EventBridge events emitted after code generation completes, clones the branch, installs dependencies, runs `make test`, uploads results to S3, and publishes a test completion event for downstream automation such as PR commenters or deployment gates.

## Key Features

- Multi-language Docker container image with Python 3.12, Java 21, Node.js 20, Terraform, and Git
- Event-driven execution via EventBridge (no Step Functions required)
- Automatic dependency detection and installation (pip, npm, maven, gradle)
- Test results and JUnit XML artifacts uploaded to S3
- Publishes pass/fail events for downstream automation to subscribe to
- Customizable container image for adding additional languages or tools
- Failure-resilient design that captures and uploads logs even on test failure

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
