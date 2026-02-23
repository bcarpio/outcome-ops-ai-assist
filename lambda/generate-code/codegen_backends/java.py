"""
Java code generator backend.

Handles code generation for Java applications with:
- Java source files (.java)
- Maven (pom.xml) or Gradle (build.gradle) dependency management
- Terraform infrastructure
- JUnit/Mockito testing patterns
"""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import re

from .base import CodeGeneratorBackend, FormattingResult, ValidationResult

logger = logging.getLogger()


class JavaCodeGenerator(CodeGeneratorBackend):
    """
    Code generator for Java applications.

    Assumes:
    - Java 17+ (LTS)
    - Maven or Gradle for builds
    - Terraform for infrastructure
    - JUnit 5 + Mockito for testing
    """

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def name(self) -> str:
        return "java"

    @property
    def display_name(self) -> str:
        return "Java"

    @property
    def source_extensions(self) -> Set[str]:
        return {".java"}

    @property
    def dependency_files(self) -> Set[str]:
        return {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}

    # =========================================================================
    # Knowledge Base Queries
    # =========================================================================

    def get_handler_standards_queries(self) -> List[str]:
        """
        Get KB queries for handler/service standards.

        These are generic queries - the KB will return framework-specific
        patterns based on what ADRs exist (Spring Boot, Lambda, Quarkus, etc.)
        """
        return [
            "Java handler standards and patterns",
            "Java error handling and exception patterns",
            "Java application entry point patterns",
        ]

    def get_testing_standards_queries(self) -> List[str]:
        """
        Get KB queries for testing standards.

        Generic JUnit queries - framework-specific patterns (Spring MockMvc,
        Lambda testing) come from repo ADRs.
        """
        return [
            "JUnit 5 testing standards and patterns",
            "Java unit testing best practices",
            "Java mocking patterns",
        ]

    def get_infrastructure_standards_queries(self) -> List[str]:
        """
        Get KB queries for infrastructure standards.

        Generic infra queries - deployment target (ECS, Lambda, K8s) comes from
        repo ADRs and project_context in .outcomeops.yaml.
        """
        return [
            "Java application deployment patterns",
            "Infrastructure as code standards",
            "Java containerization patterns",
        ]

    def get_language_standards_queries(self) -> List[str]:
        return [
            "Java coding standards and conventions",
            "Java dependency management standards",
            "Java project structure patterns",
        ]

    def get_framework_standards_queries(self) -> List[str]:
        """
        Get KB queries for framework standards.

        Generic queries - the KB returns whatever framework the repo uses
        (Spring Boot, Quarkus, Micronaut, plain Java Lambda, etc.)
        """
        return [
            "Java application framework patterns",
            "Java service layer patterns",
        ]

    # =========================================================================
    # Plan Generation Prompts
    # =========================================================================

    def get_plan_generation_system_prompt(self) -> str:
        return """You are a senior software engineer planning the implementation of a GitHub issue for a Java project.

Your task is to generate a step-by-step implementation plan that breaks down the work into small, focused steps.

CRITICAL REQUIREMENTS:
1. Each step should be completable in under 5 minutes
2. File granularity:
   - Large files (controllers, services, handlers): 1 file per step
   - Small files (DTOs, entities, exceptions): 2-3 files per step if related
   - Infrastructure: Separate step for Terraform/CloudFormation changes
3. Test step granularity (CRITICAL - prevents timeouts):
   - DO NOT create a single "Create unit tests" step
   - BREAK DOWN test creation into multiple focused steps:
     * Step N: Create unit tests for success/happy path cases (1-3 test methods)
     * Step N+1: Create unit tests for error handling (1-3 test methods)
     * Step N+2: Create integration tests (1-3 test methods)
   - Each test step should generate 1-3 test methods maximum
4. Include ONLY step-specific KB queries (general standards are already provided)
5. Build incrementally on previous steps

IMPORTANT - PROJECT STRUCTURE:
- Follow the project structure defined in the Available Standards section below
- The standards from the knowledge base will tell you where files should go
- If no specific structure is defined, use standard Maven/Gradle conventions

KB QUERY GUIDELINES:
- General standards (patterns, error handling, testing) are ALREADY available below
- ONLY include queries for step-specific context:
  - Specific patterns for this step (e.g., "JPA custom query methods")
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
      "filesToCreate": ["path/to/File.java"],
      "kbQueries": ["Step-specific query (NOT general standards)"],
      "status": "pending"
    }
  ]
}"""

    def get_code_generation_system_prompt(self) -> str:
        return """You are a senior Java developer implementing production-ready code.

Your task is to generate complete, working Java code for the files specified in the current step.

CRITICAL REQUIREMENTS:
1. Generate complete, working code that follows the standards provided
2. Follow Java best practices:
   - Use Lombok annotations to reduce boilerplate (@Data, @Builder, etc.) if available
   - Proper exception handling with custom exceptions
   - Use constructor injection where applicable
   - Follow SOLID principles

IMPORTANT - FOLLOW THE STANDARDS:
- The Available Standards section below contains project-specific patterns
- Follow the project structure, naming conventions, and patterns from those standards
- If standards specify a framework (Spring, Lambda, Quarkus), follow its conventions
- Use the testing patterns defined in the standards

MAVEN/GRADLE DEPENDENCIES:
- Use dependency management from parent POM or BOM where available
- Only declare version for non-managed dependencies
- Include only packages actually used

INFRASTRUCTURE FILES (CRITICAL):
- If existing file content is provided, you MUST ADD to it, not replace it
- Use the same patterns as existing infrastructure code
- DO NOT invent new module paths - look at existing content
- Add your new resources AFTER existing definitions

OUTPUT FORMAT (MANDATORY - DO NOT USE JSON):
You MUST use the delimiter format below. Do NOT wrap output in ```json or return {"files": [...]}.

For each file, output:
===FILE: path/to/File.java===
<raw file content here - no escaping needed>
===FILE: path/to/NextFile.java===
<next file content>
===END===

Example (follow this EXACTLY):
===FILE: src/main/java/com/example/service/UserService.java===
package com.example.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@RequiredArgsConstructor
public class UserService {
    private final UserRepository userRepository;

    public User findById(String id) {
        log.info("Finding user by id: {}", id);
        return userRepository.findById(id)
            .orElseThrow(() -> new UserNotFoundException(id));
    }
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
JAVA SPECIFIC GUIDELINES:
- Follow the project structure from the standards (may be Spring Boot, Lambda, Quarkus, etc.)
- Follow standard Maven/Gradle conventions for source layout
- Tests use JUnit 5 - follow the testing patterns from the standards
- Infrastructure changes require separate Terraform/CloudFormation step
- Use proper logging (SLF4J) and exception handling
"""

    # =========================================================================
    # File Validation
    # =========================================================================

    def validate_file(self, file_path: str, content: str) -> ValidationResult:
        """
        Validate Java files and build configuration.

        Validates:
        - pom.xml: Valid XML structure
        - build.gradle: Basic syntax check
        - .java files: Java syntax (if javac available)

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            ValidationResult
        """
        if file_path.endswith("pom.xml"):
            return self._validate_pom_xml(file_path, content)
        elif file_path.endswith((".gradle", ".gradle.kts")):
            return self._validate_gradle(file_path, content)
        elif file_path.endswith(".java"):
            return self._validate_java_syntax(file_path, content)
        else:
            # No validation for other files
            return ValidationResult(valid=True)

    def _validate_pom_xml(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate pom.xml is valid XML with required elements.
        """
        logger.info(f"[validation] Validating {file_path}")

        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(content)

            # Check for project root element (handles both with and without namespace)
            if not root.tag.endswith("project"):
                return ValidationResult(
                    valid=False,
                    error_message="pom.xml must have <project> as root element",
                    file_path=file_path,
                )

            # Get the namespace from the root tag if present
            # Root tag format: {http://maven.apache.org/POM/4.0.0}project or just project
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag[1:root.tag.index("}")]

            def find_element(parent, name):
                """Find element with or without namespace."""
                if ns:
                    elem = parent.find(f"{{{ns}}}{name}")
                    if elem is not None:
                        return elem
                return parent.find(name)

            # Check for direct children (not recursive .//), as that's the Maven convention
            group_id = find_element(root, "groupId")
            artifact_id = find_element(root, "artifactId")
            parent = find_element(root, "parent")

            # groupId is required unless inherited from parent
            if group_id is None and parent is None:
                return ValidationResult(
                    valid=False,
                    error_message="pom.xml missing groupId (required if no parent)",
                    file_path=file_path,
                )

            # artifactId is always required
            if artifact_id is None:
                return ValidationResult(
                    valid=False,
                    error_message="pom.xml missing artifactId",
                    file_path=file_path,
                )

            logger.info(f"[validation] {file_path} passed validation")
            return ValidationResult(valid=True)

        except ET.ParseError as e:
            error_msg = f"pom.xml is not valid XML: {str(e)}"
            logger.error(f"[validation] {file_path}: {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

    def _validate_gradle(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Basic validation for Gradle build files.

        Full validation would require Gradle daemon, so we just check for
        basic structure and common issues.
        """
        logger.info(f"[validation] Validating {file_path}")

        # Check for common issues
        issues = []

        # Check for unbalanced braces
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            issues.append(f"Unbalanced braces: {open_braces} open, {close_braces} close")

        # Check for unbalanced parentheses
        open_parens = content.count("(")
        close_parens = content.count(")")
        if open_parens != close_parens:
            issues.append(f"Unbalanced parentheses: {open_parens} open, {close_parens} close")

        # Check for common Kotlin DSL issues (for .kts files)
        if file_path.endswith(".kts"):
            # Check for missing quotes in string dependencies
            if re.search(r'implementation\s+[a-zA-Z]', content):
                issues.append("Kotlin DSL requires quoted strings for dependencies")

        if issues:
            error_msg = "; ".join(issues)
            logger.error(f"[validation] {file_path}: {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

        logger.info(f"[validation] {file_path} passed validation")
        return ValidationResult(valid=True)

    def _validate_java_syntax(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Validate Java syntax using javac.

        Falls back to basic validation if javac is not available.
        """
        logger.info(f"[validation] Validating {file_path}")

        # Create temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".java", delete=False
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            # Try to use javac for syntax check only
            result = subprocess.run(
                ["javac", "-Xlint:none", "-d", "/tmp", "-proc:none", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                # Extract first error line
                error_output = result.stderr or result.stdout
                error_lines = error_output.strip().split("\n")
                # Filter to actual errors
                error_msg = next(
                    (line for line in error_lines if "error:" in line.lower()),
                    error_lines[0] if error_lines else "Java compilation failed"
                )
                logger.error(f"[validation] {file_path}: {error_msg}")
                return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

            logger.info(f"[validation] {file_path} passed validation")
            return ValidationResult(valid=True)

        except FileNotFoundError:
            # javac not available, do basic validation
            logger.warning(f"[validation] javac not available, doing basic validation for {file_path}")
            return self._basic_java_validation(file_path, content)
        except subprocess.TimeoutExpired:
            error_msg = "Java validation timed out after 30s"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)
        except Exception as e:
            error_msg = f"Java validation error: {str(e)}"
            logger.error(f"[validation] {error_msg}")
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)
        finally:
            try:
                Path(tmp_path).unlink()
                # Clean up .class files
                class_file = Path(tmp_path).with_suffix(".class")
                if class_file.exists():
                    class_file.unlink()
            except Exception:
                pass

    def _basic_java_validation(
        self, file_path: str, content: str
    ) -> ValidationResult:
        """
        Basic Java validation without compiler.

        Checks for:
        - Balanced braces
        - Package declaration
        - Class/interface declaration
        """
        issues = []

        # Check for balanced braces
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            issues.append(f"Unbalanced braces: {open_braces} open, {close_braces} close")

        # Check for class or interface declaration
        if not re.search(r'\b(class|interface|enum|record)\s+\w+', content):
            issues.append("No class, interface, enum, or record declaration found")

        if issues:
            error_msg = "; ".join(issues)
            return ValidationResult(valid=False, error_message=error_msg, file_path=file_path)

        return ValidationResult(valid=True)

    # =========================================================================
    # Code Formatting
    # =========================================================================

    def get_formatters(self) -> List[Dict[str, Any]]:
        """
        Get formatters for Java Spring project.

        Returns formatters for:
        - Terraform files (.tf) - terraform fmt
        - Java files (.java) - google-java-format (if available)
        """
        return [
            {
                "name": "terraform",
                "extensions": {".tf"},
                "command": ["terraform", "fmt"],
                "timeout": 30,
            },
            {
                "name": "google-java-format",
                "extensions": {".java"},
                "command": ["google-java-format", "--replace"],
                "timeout": 60,
            },
        ]

    # =========================================================================
    # Test Generation
    # =========================================================================

    def get_test_file_pattern(self, source_file: str) -> str:
        """
        Get JUnit test file path for a source file.

        Examples:
            src/main/java/.../UserController.java -> src/test/java/.../UserControllerTest.java
            src/main/java/.../UserService.java -> src/test/java/.../UserServiceTest.java
        """
        # Replace main with test
        test_path = source_file.replace("src/main/java", "src/test/java")

        # Add Test suffix before .java
        test_path = test_path.replace(".java", "Test.java")

        return test_path

    def get_test_step_breakdown(self) -> List[str]:
        """
        Get JUnit test step breakdown.
        """
        return [
            "success/happy path cases using JUnit 5 @Test (1-3 test methods)",
            "error handling with assertThrows (1-3 test methods)",
            "integration tests with @SpringBootTest (1-3 test methods)",
        ]
