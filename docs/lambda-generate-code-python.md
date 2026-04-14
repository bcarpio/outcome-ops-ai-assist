# Generate Code: Python Backend

The Python backend powers language-aware code generation for any Python project, whether it uses Lambda, Flask, Django, FastAPI, or any other framework. It handles file validation, test patterns, and dependency management while pulling framework-specific conventions from your repository's ADRs and knowledge base rather than hardcoded prompts.

## Key Features

- Language-focused, framework-agnostic design that works with any Python project type
- Discovers code units (handlers, tests, infrastructure, schemas, shared code) from your repository structure
- Supports configurable project context via `.outcomeops.yaml` for project-specific conventions
- Incremental updates via git-based change detection to only reprocess what changed
- Automatic cleanup of stale code maps when files are deleted
- Stores code map embeddings in S3 Vectors for semantic search during code generation
- Supports custom guidelines and additional knowledge base queries per project
- Handles frontend discovery (React, Next.js, Vue) alongside Python backend code

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
