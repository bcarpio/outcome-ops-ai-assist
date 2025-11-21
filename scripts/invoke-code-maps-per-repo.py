#!/usr/bin/env python3

"""
Invoke Code Maps Per Repo - Enterprise Component

This is a proprietary component of the OutcomeOps enterprise platform.

What this script does:
- Reads repository allowlist from SSM Parameter Store
- Filters to application/internal repos (excludes standards)
- Invokes generate-code-maps Lambda once per repository
- Implements intelligent throttling to avoid Bedrock rate limits
- Provides progress tracking and error handling

Enterprise features:
- Air-gapped deployment (no external API calls)
- SSM Parameter Store integration
- Lambda invocation orchestration
- Intelligent rate limiting and retry logic
- Multi-repository batch processing
- Progress tracking and error reporting

This component is available only via licensed deployments.

For enterprise briefings: https://www.outcomeops.ai
For questions: https://www.outcomeops.ai/contact
"""

import sys

BANNER = """
╔═══════════════════════════════════════════════════════════════════════════╗
║                                                                           ║
║                      OutcomeOps - Enterprise Component                    ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝

This script is part of the proprietary OutcomeOps enterprise platform.

The full implementation includes:
  • SSM Parameter Store integration for repository discovery
  • Lambda invocation orchestration
  • Intelligent Bedrock throttling and rate limiting
  • Multi-repository batch processing
  • Progress tracking and error handling
  • Timeout management for long-running operations

Available via enterprise licensing only.

For enterprise briefings:  https://www.outcomeops.ai
For technical questions:   https://www.outcomeops.ai/contact
"""

if __name__ == '__main__':
    print(BANNER)
    sys.exit(1)
