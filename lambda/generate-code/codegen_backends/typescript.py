# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
TypeScript code generator backend.

Handles code generation for TypeScript/Node.js applications with:
- TypeScript source files (.ts, .tsx)
- package.json dependency management
- Terraform infrastructure
- Jest/Vitest testing patterns
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import CodeGeneratorBackend, FormattingResult, ValidationResult

logger = logging.getLogger()


class TypeScriptCodeGenerator(CodeGeneratorBackend):
    """
    Code generator for TypeScript/Node.js applications.

    Assumes:
    - Node.js 18+ runtime
    - TypeScript for type safety
    - Terraform for infrastructure
    - Jest or Vitest for testing
    - ESLint + Prettier for code quality
    """

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def name(self) -> str:
        return "typescript"

    @property
    def display_name(self) -> str:
        return "TypeScript"

    @property
    def source_extensions(self) -> Set[str]:
        return {".ts", ".tsx", ".js", ".jsx"}

    @property
    def dependency_files(self) -> Set[str]:
        return {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

    # =========================================================================
    # Knowledge Base Queries
    # =========================================================================

    def get_handler_standards_queries(self) -> List[str]:
        """
        Get KB queries for handler/service standards.

        These are generic queries - the KB will return framework-specific
        patterns based on what ADRs exist (Express, Lambda, Next.js, etc.)
        """
        return [
            "TypeScript handler standards and patterns",
            "Node.js error handling patterns",
            "TypeScript application entry point patterns",
        ]

    def get_testing_standards_queries(self) -> List[str]:
        """
        Get KB queries for testing standards.

        Generic Jest/Vitest queries - framework-specific patterns (supertest,
        testing-library, Lambda testing) come from repo ADRs.
        """
        return [
            "Jest testing standards and patterns",
            "TypeScript unit testing best practices",
            "Node.js mocking patterns",
        ]

    def get_infrastructure_standards_queries(self) -> List[str]:
        """
        Get KB queries for infrastructure standards.

        Generic infra queries - deployment target (Lambda, ECS, Vercel, K8s) comes from
        repo ADRs and project_context in .outcomeops.yaml.
        """
        return [
            "Node.js application deployment patterns",
            "Infrastructure as code standards",
            "Node.js containerization patterns",
        ]

    def get_language_standards_queries(self) -> List[str]:
        return [
            "TypeScript coding standards and conventions",
            "Node.js dependency management standards",
            "TypeScript project structure patterns",
        ]

    def get_framework_standards_queries(self) -> List[str]:
        """
        Get KB queries for framework standards.

        Generic queries - the KB returns whatever framework the repo uses
        (Express, Fastify, Next.js, Lambda, etc.)
        """
        return [
            "TypeScript application framework patterns",
            "Node.js service layer patterns",
        ]

    # =========================================================================
    # Plan Generation Prompts
    # =========================================================================

    def get_plan_generation_system_prompt(self) -> str:
        return """You are a senior software engineer planning the implementation of a GitHub issue for a TypeScript/Node.js project.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Large files (handlers, routes, services): 1 file per step
   - Small files (types, interfaces, DTOs): 2-3 files per step if related
   - Infrastructure: Separate step for Terraform/serverless.yml changes
3. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create unit tests" step
   - BREAK DOWN test creation into multiple focused steps:
     * Step N: Create unit tests for success/happy path cases (1-3 test functions)
     * Step N+1: Create unit tests for error handling (1-3 test functions)
     * Step N+2: Create integration tests (1-3 test functions)
   - Each test step should generate 1-3 test functions maximum
4. Include ONLY step-specific KB queries (general standards are already provided)
5. Build incrementally on previous steps

IMPORTANT - PROJECT STRUCTURE:
- Follow the project structure defined in the Available Standards section below
- The standards from the knowledge base will tell you where files should go
- If no specific structure is defined, use common TypeScript/Node.js conventions

KB QUERY GUIDELINES:
- General standards (patterns, error handling, testing) are ALREADY available below
- ONLY include queries for step-specific context:
  - Specific patterns for this step (e.g., "Zod validation for request bodies")
  - Examples relevant to this step (e.g., "file upload handling")
  - ADRs specific to this feature (e.g., "ADR for API versioning")
- DO NOT query for general standards like "error handling" or "testing best practices"

DO NOT include steps for:
- Manual verification or compliance checks
- PR review or validation steps
- "Ensure code follows patterns" type steps

Return ONLY valid JSON in this exact format:
{
  "steps": [
    {
      "stepNumber": 1,
      "title": "Brief step title",
      "description": "What this step accomplishes",
      "filesToCreate": ["path/to/file.ts"],
      "kbQueries": ["Step-specific query (NOT general standards)"],
      "status": "pending"
    }
  ]
}"""

    def get_code_generation_system_prompt(self) -> str:
        return """You are a senior TypeScript developer implementing production-ready code.

Your task is to generate complete, working TypeScript code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Follow TypeScript best practices:
   - Strict TypeScript (noImplicitAny, strictNullChecks)
   - Proper error handling with custom error classes
   - Use async/await, never callbacks
   - No any types unless absolutely necessary

IMPORTANT - FOLLOW THE STANDARDS:
- The Available Standards section below contains project-specific patterns
- Follow the project structure, naming conventions, and patterns from those standards
- If standards specify a framework (Express, Lambda, Next.js, Fastify), follow its conventions
- Use the testing patterns defined in the standards

PACKAGE.JSON:
- Use exact versions (^1.2.3 is acceptable for minor updates)
- Separate dependencies and devDependencies
- Include required @types/* packages
- Validate packages exist on npm before including

INFRASTRUCTURE FILES (CRITICAL):
- If existing file content is provided, you MUST ADD to it, not replace it
- Use the same patterns as existing infrastructure code (serverless.yml, terraform, etc.)
- DO NOT invent new module paths - look at existing content
- Add your new resources AFTER existing definitions

OUTPUT FORMAT (MANDATORY - DO NOT USE JSON):
You MUST use the delimiter format below. Do NOT wrap output in ```json or return {"files": [...]}.

For each file, output:
===FILE: path/to/file.ts===
<raw file content here - no escaping needed>
===FILE: path/to/next_file.ts===
<next file content>
===END===

Example (follow this EXACTLY):
===FILE: src/services/userService.ts===
import { logger } from '../utils/logger';
import { UserRepository } from '../repositories/userRepository';

export class UserService {
  constructor(private readonly userRepository: UserRepository) {}

  async findById(id: string): Promise<User> {
    logger.info({ id }, 'Finding user by id');
    const user = await this.userRepository.findById(id);
    if (!user) {
      throw new UserNotFoundError(id);
    }
    return user;
  }
}
===FILE: src/types/user.ts===
export interface User {
  id: string;
  name: string;
}
===END===

CRITICAL RULES:
- Start your response with ===FILE: path=== (no preamble, no JSON)
- Output raw code exactly as it should appear in the file
- NO JSON wrapping, NO ```json blocks, NO escaping, NO base64
- Each file starts with ===FILE: path=== on its own line
- End with ===END=== on its own line
- The path after FILE: must match the expected file path exactly"""

    def get_plan_generation_guidelines(self) -> str:
        return """
TYPESCRIPT SPECIFIC GUIDELINES:
- Follow the project structure from the standards (may be Express, Lambda, Next.js, etc.)
- Use strict TypeScript configuration
- Tests use Jest or Vitest - follow the testing patterns from the standards
- Infrastructure changes require separate Terraform/serverless.yml step
- Use proper error handling with typed errors
"""

    # =========================================================================
    # File Validation
    # =========================================================================

    def validate_file(self, file_path: str, content: str) -> ValidationResult:
        """
        Validate TypeScript files and package.json.

        Validates:
        - package.json: Valid JSON structure
        - .ts/.tsx files: TypeScript syntax (if tsc available)

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            ValidationResult
        """
        if file_path.endswith("package.json"):
            return self._validate_package_json(file_path, content)
        elif file_path.endswith((".ts", ".tsx")):
            return self._validate_typescript_syntax(file_path, content)
        else:
            # No validation for other files
            return ValidationResult(valid=True)

    def _validate_package_json(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate package.json is valid JSON with required fields.
        """
        logger.info(f"[validation] Validating {file_path}")

        try:
            pkg = json.loads(content)

            # Check required fields
            if "name" not in pkg:
                return ValidationResult(
                    valid=False,
                    error_message="package.json missing 'name' field",
                    file_path=file_path,
                )

            # Validate dependencies are objects if present
            for dep_field in ["dependencies", "devDependencies", "peerDependencies"]:
                if dep_field in pkg and not isinstance(pkg[dep_field], dict):
                    return ValidationResult(
                        valid=False,
                        error_message=f"package.json '{dep_field}' must be an object",
                        file_path=file_path,
                    )

            logger.info(f"[validation] {file_path} passed validation")
            return ValidationResult(valid=True)

        except json.JSONDecodeError as e:
            error_msg = f"package.json is not valid JSON: {e.msg}"
            logger.error(f"[validation] {file_path}: {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

    def _validate_typescript_syntax(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate TypeScript syntax using tsc --noEmit.

        Falls back to basic validation if tsc is not available.
        """
        logger.info(f"[validation] Validating {file_path}")

        # Create temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ts", delete=False
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            # Try to use tsc for validation
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--skipLibCheck", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                # Extract first error line
                error_lines = result.stderr.strip().split("\n")
                error_msg = error_lines[0] if error_lines else "TypeScript compilation failed"
                logger.error(f"[validation] {file_path}: {error_msg}")
                return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

            logger.info(f"[validation] {file_path} passed validation")
            return ValidationResult(valid=True)

        except FileNotFoundError:
            # tsc not available, skip validation
            logger.warning(f"[validation] tsc not available, skipping validation for {file_path}")
            return ValidationResult(valid=True)
        except subprocess.TimeoutExpired:
            error_msg = "TypeScript validation timed out after 30s"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)
        except Exception as e:
            error_msg = f"TypeScript validation error: {str(e)}"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)
        finally:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass

    # =========================================================================
    # Code Formatting
    # =========================================================================

    def get_formatters(self) -> List[Dict[str, Any]]:
        """
        Get formatters for TypeScript Express project.

        Returns formatters for:
        - Terraform files (.tf) - terraform fmt
        - TypeScript/JS files - prettier (if available)
        """
        return [
            {
                "name": "terraform",
                "extensions": {".tf"},
                "command": ["terraform", "fmt"],
                "timeout": 30,
            },
            {
                "name": "prettier",
                "extensions": {".ts", ".tsx", ".js", ".jsx", ".json"},
                "command": ["npx", "prettier", "--write"],
                "timeout": 30,
            },
        ]

    # =========================================================================
    # Test Generation
    # =========================================================================

    def get_test_file_pattern(self, source_file: str) -> str:
        """
        Get Jest/Vitest test file path for a source file.

        Examples:
            src/routes/users.ts -> tests/unit/users.test.ts
            src/services/userService.ts -> tests/unit/userService.test.ts
        """
        # Extract file name
        file_name = Path(source_file).stem

        # Determine test type based on source location
        if "routes" in source_file:
            return f"tests/integration/{file_name}.test.ts"
        else:
            return f"tests/unit/{file_name}.test.ts"

    def get_test_step_breakdown(self) -> List[str]:
        """
        Get Jest/Vitest test step breakdown.
        """
        return [
            "success/happy path cases using Jest/Vitest (1-3 test functions)",
            "error handling with expect().rejects (1-3 test functions)",
            "integration tests with supertest (1-3 test functions)",
        ]
