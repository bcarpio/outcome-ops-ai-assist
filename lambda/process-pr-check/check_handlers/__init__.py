"""Check handlers package for PR analysis."""

from .adr_compliance import check_adr_compliance
from .architectural_duplication import check_architectural_duplication
from .breaking_changes import check_breaking_changes
from .readme_freshness import check_readme_freshness
from .test_coverage import check_test_coverage

__all__ = [
    "check_adr_compliance",
    "check_architectural_duplication",
    "check_breaking_changes",
    "check_readme_freshness",
    "check_test_coverage",
]
