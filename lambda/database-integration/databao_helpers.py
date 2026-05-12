"""
Thin wrapper around Databao Context Engine calls.

Keeps the Databao API surface contained to one module so future swaps
or version bumps are cheap. All exported functions accept generic
engine + credential params and translate to Databao datasource config
shapes internally.

This module is intentionally light on logic — Databao does the real
work. The wrappers exist so the integration / sync handlers don't
import `databao_context_engine` directly and the surface stays auditable.

NOTE (chunk 01): the Databao layer is wired in chunk 02. Until then,
the `databao_context_engine` import is deferred so the integration
Lambda can import this module cleanly even without the layer present
(e.g., in unit tests). When the layer is missing, these functions
raise DatabaoLayerNotAttached at call time.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DatabaoLayerNotAttached(RuntimeError):
    """Raised when Databao is called but the Lambda layer isn't attached.

    Catch-and-audit per ADR-019 — the sync Lambda should emit a
    LICENSE_FAIL_OPEN-style "deploy bug" audit row when it sees this.
    """


@dataclass
class ValidationResult:
    """Result of a connection-validation attempt."""
    valid: bool
    error: Optional[str] = None
    server_version: Optional[str] = None


@dataclass
class SchemaInfo:
    """One catalog/schema/table entry returned by the picker."""
    catalog: str
    schema: str
    table: str
    row_count_estimate: Optional[int] = None


def _import_databao():
    """Deferred import. Raises DatabaoLayerNotAttached if the layer
    isn't on the Lambda search path."""
    try:
        import databao_context_engine  # noqa: F401
        return databao_context_engine
    except ImportError as e:
        raise DatabaoLayerNotAttached(
            "databao_context_engine import failed - layer not attached "
            "(see terraform/lambda-databao-layer.tf): " + str(e)
        )


def validate_connection(
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_payload: Dict[str, Any],
) -> ValidationResult:
    """Open a connection, run a trivial query, close. Used by the UI
    "Test connection" button before persisting credentials, and by the
    integration handler at connection-create time.

    Args:
        engine: "postgres" | "mysql" | "mssql"
        host, port, database_name: connection target
        credential_payload: engine-specific auth dict (see
            credential_manager.py for shapes)

    Returns:
        ValidationResult with valid=True and server_version on success,
        or valid=False with an error message on failure.
    """
    dce = _import_databao()
    try:
        # The actual Databao API surface for this is:
        #   DatabaoContextDomainManager().check_datasource_connection()
        # The full wiring lands when the layer is in place; this stub
        # provides the call-site shape so the integration handler can
        # be unit-tested with mocked validate_connection.
        raise NotImplementedError(
            "Databao validate_connection wiring lands with the layer in chunk 02"
        )
    except NotImplementedError:
        # Stub during chunk 01 — return a sentinel so callers don't
        # crash. Real impl returns a ValidationResult.
        return ValidationResult(
            valid=False,
            error="Databao layer wiring pending (chunk 02)",
        )


def list_schemas(
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_payload: Dict[str, Any],
) -> List[SchemaInfo]:
    """Metadata-only enumeration for the UI schema/table picker.

    Hits INFORMATION_SCHEMA (or engine-equivalent) and returns the
    catalog/schema/table tree without column-level introspection or
    sampling. Cheap enough to call on every picker-modal open for
    small/medium DBs.

    For very large warehouses we may need to add a cache layer
    (DDB-backed) — flagged in plan/PROGRESS.md as an open question.
    """
    dce = _import_databao()
    try:
        raise NotImplementedError(
            "Databao list_schemas wiring lands with the layer in chunk 02"
        )
    except NotImplementedError:
        return []


# build_context lives in database-sync (heavy operation, long timeout
# Lambda needed). Not exposed here.
