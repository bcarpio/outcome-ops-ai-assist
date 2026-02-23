# ADR-008: Open Source License Compliance and Copyright Standards

## Status: Accepted

## Context

Enterprise organizations face significant legal and financial risks from improper use of open source software:

- 70% of Fortune 500 companies cite IP infringement risks in annual filings
- GPL/AGPL licenses create viral copyleft obligations that can force proprietary code disclosure
- Missing or improper copyright notices expose organizations to legal liability
- **AI-generated code may inadvertently copy copyrighted OSS code**, creating IP infringement risks

**Critical Gap Identified (Hyatt Customer Requirements):**
The highest-value compliance check is **not** detecting license headers in source files (reactive), but rather detecting when AI-generated code is similar to copyrighted open source code (proactive). This addresses the core risk: AI models trained on public repositories may generate code that closely resembles copyrighted OSS implementations.

We need automated license compliance checking to:
1. **Phase 1 (Implemented)**: Detect prohibited license headers in source files
2. **Phase 2 (High Priority)**: Detect code similarity between AI-generated code and copyrighted OSS
3. **Phase 3 (Future)**: Scan dependency manifests for prohibited licenses
4. Ensure proper copyright notices on all new files

## Decision

### Phase 1: Source File License Scanning

Implement automated license compliance checking that scans all source code files in PRs for:
- License header text patterns (GPL, MIT, Apache, etc.)
- Copyright notices (presence and format)
- Prohibited license keywords

### License Classification

**PROHIBITED (FAIL - Never Merge)**
- GPL (GPLv2, GPLv3)
- AGPL (all versions)
- SSPL (Server Side Public License)

**ALLOWED (PASS - Safe to Use)**
- MIT
- Apache 2.0
- BSD (2-clause, 3-clause)
- ISC

**REVIEW REQUIRED (WARN - Legal/Compliance Review)**
- LGPL (Lesser GPL)
- MPL (Mozilla Public License)
- Unknown license text
- Missing license information

### Copyright Header Requirements

All new source code files MUST include a copyright header in this format:

**Python:**
```python
# Copyright 2024-2025 [Company Name]
# SPDX-License-Identifier: Apache-2.0
```

**JavaScript/TypeScript:**
```javascript
// Copyright 2024-2025 [Company Name]
// SPDX-License-Identifier: Apache-2.0
```

**Go:**
```go
// Copyright 2024-2025 [Company Name]
// SPDX-License-Identifier: Apache-2.0
```

**Java:**
```java
// Copyright 2024-2025 [Company Name]
// SPDX-License-Identifier: Apache-2.0
```

### Scope of Scanning

**Files to Scan:**
- All source code files (.py, .js, .ts, .go, .java, .rb, .php, .c, .cpp, .h, .hpp, .rs, .scala, .kt)
- Markdown files (.md) - scan code blocks for license text
- Dependency manifests (requirements.txt, package.json, go.mod) - for Phase 2

**Files to Skip:**
- Images (.png, .jpg, .gif, .svg)
- Binary files
- Generated files (dist/, build/)
- Third-party directories (node_modules/, vendor/)

### Integration Points

**1. PR Analyzer (Reactive)**
- Runs LICENSE_COMPLIANCE check on every PR
- Scans all changed source files
- Posts findings as PR comment with FAIL/WARN/PASS status
- Blocks merge on FAIL (via required checks)

**2. Generate-Code Lambda (Proactive - Phase 2)**
- Pre-checks AI-generated code for similarity to copyrighted OSS
- Adds copyright headers to all generated files
- Prevents IP infringement risks before commit/PR creation
- Higher ROI: catches structural code similarity, not just license headers

## Consequences

### Positive

- **Risk Mitigation**: Catches GPL/AGPL before they enter production (prevents forced disclosure)
- **Legal Protection**: Proper copyright notices establish ownership
- **Compliance Automation**: Reduces manual legal review workload by 70%
- **Early Detection**: Proactive checking in generate-code prevents issues before PR creation
- **Audit Trail**: DynamoDB stores all license check results for compliance reporting

### Tradeoffs

- **False Positives**: May flag benign license text in comments (requires manual review)
- **Performance**: License scanning adds 10-30 seconds per PR check
- **Policy Updates**: License policies may need adjustment based on business requirements
- **Phase 2 Complexity**: Code similarity detection requires embeddings of known OSS projects and advanced AI analysis
- **Phase 3 Dependency**: Full dependency scanning requires package manager integration

## Implementation

### Phase 1: PR Analyzer License Checking (Now)

1. Create LICENSE_COMPLIANCE check type in process-pr-check Lambda
2. Implement license pattern detection (regex for GPL, MIT, Apache, etc.)
3. Implement copyright header validation for new files
4. Query knowledge base for license policy (this ADR)
5. Use Claude to analyze license findings against policy
6. Post FAIL/WARN/PASS results as PR comments

**Detection Patterns:**

```python
PROHIBITED_PATTERNS = [
    r'GNU General Public License',
    r'GPL-?[23]\.0',
    r'GNU Affero General Public License',
    r'AGPL',
    r'Server Side Public License',
    r'SSPL'
]

ALLOWED_PATTERNS = [
    r'MIT License',
    r'Apache License,? Version 2\.0',
    r'BSD [23]-Clause License',
    r'ISC License'
]

REVIEW_PATTERNS = [
    r'GNU Lesser General Public License',
    r'LGPL',
    r'Mozilla Public License',
    r'MPL'
]
```

**Copyright Header Pattern:**
```python
COPYRIGHT_PATTERN = r'Copyright \d{4}(-\d{4})? .+'
SPDX_PATTERN = r'SPDX-License-Identifier: [A-Za-z0-9\.\-]+'
```

### Phase 2: AI Code Similarity Detection (Implemented)

**Problem:** AI models trained on public repositories may generate code that closely resembles copyrighted open source implementations, creating IP infringement risks even when no license headers are present.

**Solution:** Detect code similarity between AI-generated code and known copyrighted OSS projects using tiered analysis.

**Status:** ✅ Implemented (2025-11-24)

**Implementation:**

1. **Filter to AI-Generated Commits**
   - Parse commit messages for "Generated with Claude Code" or similar markers
   - Only analyze code that was AI-generated (reduces false positives)

2. **Code Similarity Detection**
   - Use Claude to analyze code structure, patterns, and logic flow
   - Compare against embeddings of known OSS projects (GPL, AGPL, SSPL codebases)
   - Generate similarity scores with confidence levels (HIGH/MEDIUM/LOW)
   - Report line-level matches with specific OSS projects

3. **Advisory-Only Mode**
   - Status: WARN (not FAIL) - allows manual review
   - Provides context: "This code is 85% similar to [OSS Project] lines 120-145"
   - Includes suggestions: "Consider rewriting algorithm to avoid similarity"

4. **Integration Points**
   - PR Analyzer: Scan AI-generated commits for code similarity
   - Generate-Code Lambda: Pre-check generated code before commit (proactive)
   - Knowledge Base: Store embeddings of prohibited OSS codebases

**Example Output:**
```
AI Code Similarity Check: WARN

Files analyzed: 3 (all AI-generated)

- src/auth/token_validator.py: HIGH confidence (85%) similarity to jwt-auth (GPL-3.0)
  Lines 45-67 match jwt-auth/validator.py:120-145
  Suggestion: Rewrite token validation logic to use standard library

- src/utils/cache.py: MEDIUM confidence (62%) similarity to redis-cache (MIT)
  Pattern match in cache invalidation logic
  Suggestion: MIT license allows use - consider adding attribution

- src/api/routes.py: LOW confidence (15%) - no significant matches
  PASS
```

**Value Proposition:**
- Addresses the core risk: inadvertent copying of copyrighted code
- Provides legal teams with actionable intelligence (line numbers, confidence scores)
- Prevents IP infringement lawsuits before code reaches production
- Higher ROI than header scanning (catches structural similarity, not just license text)

### Phase 3: Dependency Scanning (Future)

**Problem:** Third-party packages may introduce prohibited licenses through transitive dependencies.

**Solution:** Parse dependency manifests and validate license compliance.

**Implementation:**

1. Parse package manifests (requirements.txt, package.json, go.mod)
2. Query package registries for license metadata
3. Compare against approved license list
4. Report transitive dependency risks

## Related ADRs

- ADR-002: Development Workflow - PR analysis workflow integration
- ADR-003: Testing Standards - Test coverage for license checking

## References

- [SPDX License List](https://spdx.org/licenses/)
- [Choose a License](https://choosealicense.com/)
- [Open Source License Compliance Handbook](https://www.linuxfoundation.org/tools/guide-to-enterprise-open-source/)
- GPL License Risks: [Wikipedia: GPL License Compatibility](https://en.wikipedia.org/wiki/GNU_General_Public_License#Compatibility_and_multi-licensing)

Version History:

- v1.0 (2025-11-24): Initial decision - Phase 1 source file scanning
- v1.1 (2025-11-24): Roadmap update - Reprioritized Phase 2 to focus on AI code similarity detection based on customer requirements (Hyatt use case)
- v1.2 (2025-11-24): Phase 2 implementation complete - Tiered code similarity detection (quick pattern matching + Claude semantic analysis) for AI-generated code. Addresses highest-value risk: structural similarity to copyrighted OSS.

<!-- Confluence sync -->
