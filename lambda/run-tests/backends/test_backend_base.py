"""
Base class for run-tests backends.

Each backend handles test execution for a specific language/framework:
- Setting up the test environment (dependencies, tools)
- Running tests with the appropriate command
- Collecting test results (JUnit XML, coverage reports)
- Classifying and potentially auto-fixing failures
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result from running a shell command."""
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        return f"{self.stdout}\n{self.stderr}".strip()


@dataclass
class TestResult:
    """Result from running tests."""
    passed: bool
    exit_code: int
    output: str
    failure_type: Optional[str] = None  # "import_error", "syntax_error", "logic_error"
    failure_reason: Optional[str] = None
    junit_content: Optional[str] = None
    coverage_percent: Optional[float] = None
    command_results: List[CommandResult] = field(default_factory=list)


class TestBackend(ABC):
    """
    Abstract base class for test backends.

    Subclasses implement language/framework-specific test execution:
    - PythonBackend: pytest with virtualenv
    - JavaBackend: Gradle/Maven test runners
    - TypeScriptBackend: npm/yarn test runners
    """

    def __init__(self, repo_dir: str, config: Dict[str, Any]):
        """
        Initialize test backend.

        Args:
            repo_dir: Path to the cloned repository
            config: Configuration from .outcomeops.yaml
        """
        self.repo_dir = repo_dir
        self.config = config
        self.command_results: List[CommandResult] = []

    @abstractmethod
    def get_backend_name(self) -> str:
        """Return human-readable backend name."""
        pass

    @abstractmethod
    def get_backend_type(self) -> str:
        """Return backend type identifier (python, java, typescript)."""
        pass

    @abstractmethod
    def setup_environment(self) -> bool:
        """
        Set up the test environment (install dependencies, etc.).

        Returns:
            True if setup succeeded, False otherwise
        """
        pass

    @abstractmethod
    def get_test_command(self) -> List[str]:
        """
        Get the command to run tests.

        Returns:
            Command as list of strings (e.g., ["./gradlew", "test"])
        """
        pass

    @abstractmethod
    def get_junit_paths(self) -> List[Path]:
        """
        Get paths where JUnit XML results might be found.

        Returns:
            List of possible JUnit result paths
        """
        pass

    def run_tests(self, max_attempts: int = 3) -> TestResult:
        """
        Run tests with optional auto-fix retry loop.

        Args:
            max_attempts: Maximum number of test attempts (including retries)

        Returns:
            TestResult with test outcome
        """
        # Setup environment first
        if not self.setup_environment():
            return TestResult(
                passed=False,
                exit_code=1,
                output="Environment setup failed",
                failure_type="setup_error",
                failure_reason="Failed to set up test environment",
                command_results=self.command_results
            )

        test_command = self.get_test_command()
        attempt = 0
        last_result = None

        while attempt < max_attempts:
            logger.info(f"[{self.get_backend_type()}] Test attempt {attempt + 1}/{max_attempts}")

            result = self._run_command(test_command)
            last_result = result

            if result.succeeded:
                logger.info(f"[{self.get_backend_type()}] Tests passed!")
                return TestResult(
                    passed=True,
                    exit_code=0,
                    output=result.output,
                    junit_content=self._read_junit_results(),
                    command_results=self.command_results
                )

            # Classify failure
            failure_type = self.classify_failure(result.output)
            logger.info(f"[{self.get_backend_type()}] Test failure: {failure_type}")

            # Don't retry logic errors
            if failure_type == "logic_error" or attempt >= max_attempts - 1:
                break

            # Attempt auto-fix
            if not self.attempt_auto_fix(failure_type, result.output):
                logger.warning(f"[{self.get_backend_type()}] Auto-fix failed")
                break

            attempt += 1

        # Tests failed
        return TestResult(
            passed=False,
            exit_code=last_result.exit_code if last_result else 1,
            output=last_result.output if last_result else "",
            failure_type=self.classify_failure(last_result.output) if last_result else "unknown",
            failure_reason=f"Tests failed after {attempt + 1} attempts",
            junit_content=self._read_junit_results(),
            command_results=self.command_results
        )

    def classify_failure(self, output: str) -> str:
        """
        Classify test failure type.

        Override in subclasses for language-specific classification.

        Args:
            output: Combined stdout/stderr from test run

        Returns:
            Failure type: "import_error", "syntax_error", or "logic_error"
        """
        output_lower = output.lower()

        # Import/module errors
        if any(pattern in output_lower for pattern in [
            "modulenotfounderror",
            "importerror",
            "no module named",
            "cannot find module",
            "module not found"
        ]):
            return "import_error"

        # Syntax errors
        if any(pattern in output_lower for pattern in [
            "syntaxerror",
            "syntax error",
            "unexpected token",
            "parsing error"
        ]):
            return "syntax_error"

        # Default to logic error (assertion failures, etc.)
        return "logic_error"

    def attempt_auto_fix(self, failure_type: str, output: str) -> bool:
        """
        Attempt to auto-fix a test failure.

        Override in subclasses for language-specific fixes.

        Args:
            failure_type: Type of failure from classify_failure()
            output: Test output

        Returns:
            True if fix was applied, False otherwise
        """
        # Base class doesn't implement auto-fix
        return False

    def _run_command(
        self,
        command: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 900
    ) -> CommandResult:
        """Run a shell command and capture output."""
        cwd = cwd or self.repo_dir
        start = time.monotonic()

        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
            duration = time.monotonic() - start
            result = CommandResult(
                command=command,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=duration
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            result = CommandResult(
                command=command,
                exit_code=124,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + f"\nCommand timed out after {timeout}s",
                duration_seconds=duration
            )

        self.command_results.append(result)
        logger.info(f"Command finished ({result.duration_seconds:.2f}s): {' '.join(command)}")
        return result

    def _read_junit_results(self) -> Optional[str]:
        """Read JUnit XML results if available."""
        for junit_path in self.get_junit_paths():
            full_path = Path(self.repo_dir) / junit_path
            if full_path.exists():
                try:
                    return full_path.read_text()
                except Exception as e:
                    logger.warning(f"Failed to read JUnit results from {junit_path}: {e}")
        return None
