# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
React code generator backend.

Handles code generation for React/Frontend applications with:
- React functional components (.tsx, .jsx)
- Custom hooks and Context API
- React Testing Library patterns
- Next.js/Vite project structure
- Tailwind CSS/styled-components
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import CodeGeneratorBackend, FormattingResult, ValidationResult

logger = logging.getLogger()


class ReactCodeGenerator(CodeGeneratorBackend):
    """
    Code generator for React frontend applications.

    Assumes:
    - React 18+ with functional components
    - TypeScript for type safety
    - Next.js or Vite for build tooling
    - React Testing Library for testing
    - Tailwind CSS or styled-components for styling
    """

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def name(self) -> str:
        return "react"

    @property
    def display_name(self) -> str:
        return "React"

    @property
    def source_extensions(self) -> Set[str]:
        return {".tsx", ".jsx", ".ts", ".js", ".css", ".scss"}

    @property
    def dependency_files(self) -> Set[str]:
        return {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

    # =========================================================================
    # Knowledge Base Queries
    # =========================================================================

    def get_handler_standards_queries(self) -> List[str]:
        """
        Get KB queries for React component standards.

        These are queries for component patterns - the KB will return
        project-specific patterns based on ADRs.
        """
        return [
            "React component standards and patterns",
            "React functional component best practices",
            "React props and state management patterns",
        ]

    def get_testing_standards_queries(self) -> List[str]:
        """
        Get KB queries for React testing standards.

        Focuses on React Testing Library patterns.
        """
        return [
            "React Testing Library standards and patterns",
            "React component testing best practices",
            "React hook testing patterns",
        ]

    def get_infrastructure_standards_queries(self) -> List[str]:
        """
        Get KB queries for frontend infrastructure.

        Covers deployment, build, and hosting patterns.
        """
        return [
            "Frontend deployment patterns",
            "React application hosting standards",
            "Static site generation patterns",
        ]

    def get_language_standards_queries(self) -> List[str]:
        return [
            "TypeScript coding standards for React",
            "React TypeScript best practices",
            "JSX/TSX coding conventions",
        ]

    def get_framework_standards_queries(self) -> List[str]:
        """
        Get KB queries for React framework standards.

        Covers Next.js, React Router, state management.
        """
        return [
            "React framework patterns",
            "Next.js application structure",
            "React state management patterns",
        ]

    # =========================================================================
    # Plan Generation Prompts
    # =========================================================================

    def get_plan_generation_system_prompt(self) -> str:
        return """You are a senior frontend engineer planning the implementation of a GitHub issue for a React/TypeScript project.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Components: 1 component per step (including its styles and types)
   - Hooks: 1-2 related hooks per step
   - Context/Providers: 1 context per step
   - Tests: Separate step per test category
3. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create tests" step
   - BREAK DOWN test creation into multiple focused steps:
     * Step N: Create component render tests (1-3 test functions)
     * Step N+1: Create interaction tests (click, input) (1-3 test functions)
     * Step N+2: Create integration tests (1-3 test functions)
   - Each test step should generate 1-3 test functions maximum
4. Include ONLY step-specific KB queries (general standards are already provided)
5. Build incrementally on previous steps

REACT COMPONENT STRUCTURE:
- Each component should be in its own directory when it has multiple files:
  components/Button/
    Button.tsx          # Main component
    Button.test.tsx     # Tests
    Button.module.css   # Styles (if using CSS modules)
    index.ts           # Barrel export
- Simple components can be single files: components/Button.tsx

KB QUERY GUIDELINES:
- General standards (patterns, testing, styling) are ALREADY available below
- ONLY include queries for step-specific context:
  - Specific patterns for this step (e.g., "form validation patterns")
  - Examples relevant to this step (e.g., "modal accessibility patterns")
  - ADRs specific to this feature
- DO NOT query for general standards like "component best practices"

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
      "filesToCreate": ["path/to/Component.tsx"],
      "kbQueries": ["Step-specific query (NOT general standards)"],
      "status": "pending"
    }
  ]
}"""

    def get_code_generation_system_prompt(self) -> str:
        return """You are a senior React developer implementing production-ready frontend code.

Your task is to generate complete, working React code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Follow React best practices:
   - Functional components with hooks (NO class components)
   - Proper TypeScript types for props, state, and events
   - Use React.FC sparingly - prefer explicit return types
   - Destructure props in function signature
   - Use named exports (not default exports)

COMPONENT PATTERNS:
- Props interface above component: interface ButtonProps { ... }
- Component signature: export function Button({ label, onClick }: ButtonProps): JSX.Element
- Use semantic HTML elements
- Include ARIA attributes for accessibility
- Handle loading and error states

HOOKS PATTERNS:
- Custom hooks start with "use": useAuth, useFetch, useLocalStorage
- Return typed values: [value, setValue] or { data, loading, error }
- Handle cleanup in useEffect return

TESTING PATTERNS (React Testing Library):
- Import: import { render, screen, fireEvent } from '@testing-library/react'
- Use screen.getByRole, screen.getByText over getByTestId
- Test user behavior, not implementation details
- Use userEvent over fireEvent when possible
- Wrap state updates in act() or use findBy queries

STYLING:
- Tailwind CSS: Use className with Tailwind utilities
- CSS Modules: Import styles from './Component.module.css'
- styled-components: Use tagged template literals

OUTPUT FORMAT (MANDATORY - DO NOT USE JSON):
You MUST use the delimiter format below. Do NOT wrap output in ```json or return {"files": [...]}.

For each file, output:
===FILE: path/to/file.tsx===
<raw file content here - no escaping needed>
===FILE: path/to/next_file.tsx===
<next file content>
===END===

Example (follow this EXACTLY):
===FILE: src/components/Button/Button.tsx===
import { type MouseEvent } from 'react';

interface ButtonProps {
  label: string;
  onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  variant?: 'primary' | 'secondary';
  disabled?: boolean;
}

export function Button({
  label,
  onClick,
  variant = 'primary',
  disabled = false,
}: ButtonProps): JSX.Element {
  return (
    <button
      type="button"
      className={`btn btn-${variant}`}
      onClick={onClick}
      disabled={disabled}
      aria-disabled={disabled}
    >
      {label}
    </button>
  );
}
===FILE: src/components/Button/Button.test.tsx===
import { render, screen, fireEvent } from '@testing-library/react';
import { Button } from './Button';

describe('Button', () => {
  it('renders with label', () => {
    render(<Button label="Click me" onClick={() => {}} />);
    expect(screen.getByRole('button', { name: 'Click me' })).toBeInTheDocument();
  });

  it('calls onClick when clicked', () => {
    const handleClick = jest.fn();
    render(<Button label="Click me" onClick={handleClick} />);
    fireEvent.click(screen.getByRole('button'));
    expect(handleClick).toHaveBeenCalledTimes(1);
  });
});
===FILE: src/components/Button/index.ts===
export { Button } from './Button';
export type { ButtonProps } from './Button';
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
REACT SPECIFIC GUIDELINES:
- Follow the project structure from the standards (Next.js, Vite, CRA)
- Use TypeScript for all components
- Tests use React Testing Library with Jest or Vitest
- Include accessibility (a11y) considerations
- Use proper TypeScript types (no 'any')
- Consider loading and error states for async operations
- Include barrel exports (index.ts) for component directories
"""

    # =========================================================================
    # File Validation
    # =========================================================================

    def validate_file(self, file_path: str, content: str) -> ValidationResult:
        """
        Validate React/TypeScript files.

        Validates:
        - package.json: Valid JSON structure
        - .tsx/.jsx files: Basic syntax validation
        - .ts/.js files: TypeScript syntax (if tsc available)

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            ValidationResult
        """
        if file_path.endswith("package.json"):
            return self._validate_package_json(file_path, content)
        elif file_path.endswith((".tsx", ".jsx")):
            return self._validate_react_syntax(file_path, content)
        elif file_path.endswith((".ts", ".js")):
            return self._validate_typescript_syntax(file_path, content)
        else:
            # No validation for other files (CSS, etc.)
            return ValidationResult(valid=True)

    def _validate_package_json(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """Validate package.json is valid JSON with required fields."""
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

    def _validate_react_syntax(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate React/JSX/TSX syntax.

        Basic validation for React component files.
        """
        logger.info(f"[validation] Validating {file_path}")

        # Basic checks for common React issues
        issues = []

        # Check for common JSX issues
        if content.count("<") != content.count(">"):
            # This could be a false positive with generics, so just warn
            logger.warning(f"[validation] {file_path}: Unbalanced angle brackets (may be false positive)")

        # Check for import React (needed in some configs)
        # Not an error since React 17+ doesn't require it

        # Try TypeScript validation if available
        return self._validate_typescript_syntax(file_path, content)

    def _validate_typescript_syntax(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate TypeScript syntax using tsc --noEmit.

        Falls back to basic validation if tsc is not available.
        """
        logger.info(f"[validation] Validating TypeScript syntax: {file_path}")

        # Determine file suffix based on original extension
        suffix = ".tsx" if file_path.endswith((".tsx", ".jsx")) else ".ts"

        # Create temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            # Try to use tsc for validation
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--skipLibCheck", "--jsx", "react-jsx", tmp_path],
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
        Get formatters for React project.

        Returns formatters for:
        - TypeScript/JSX files - prettier
        - CSS files - prettier
        - Terraform files (if any) - terraform fmt
        """
        return [
            {
                "name": "prettier",
                "extensions": {".ts", ".tsx", ".js", ".jsx", ".json", ".css", ".scss"},
                "command": ["npx", "prettier", "--write"],
                "timeout": 30,
            },
            {
                "name": "terraform",
                "extensions": {".tf"},
                "command": ["terraform", "fmt"],
                "timeout": 30,
            },
        ]

    # =========================================================================
    # Test Generation
    # =========================================================================

    def get_test_file_pattern(self, source_file: str) -> str:
        """
        Get React Testing Library test file path for a source file.

        Examples:
            src/components/Button/Button.tsx -> src/components/Button/Button.test.tsx
            src/hooks/useAuth.ts -> src/hooks/useAuth.test.ts
        """
        # Get file name and extension
        path = Path(source_file)
        name = path.stem
        ext = path.suffix

        # Determine test extension (keep .tsx for components, .ts for hooks/utils)
        test_ext = ".tsx" if ext in [".tsx", ".jsx"] else ".ts"

        # Generate test file path in same directory
        test_name = f"{name}.test{test_ext}"
        return str(path.parent / test_name)

    def get_test_step_breakdown(self) -> List[str]:
        """
        Get React Testing Library test step breakdown.
        """
        return [
            "component rendering and structure tests (1-3 test functions)",
            "user interaction tests using fireEvent/userEvent (1-3 test functions)",
            "async behavior and state change tests (1-3 test functions)",
        ]


# Register the backend
from .factory import register_backend
register_backend("react", ReactCodeGenerator)
