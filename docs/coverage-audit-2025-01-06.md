# Test Coverage Audit - January 6, 2025

**Current Coverage: 55% (2157 statements, 960 missed)**
**Target Coverage: 80%**
**Gap: 25 percentage points (540 additional statements to cover)**

## Executive Summary

To reach 80% coverage, we need to add tests for **540 additional statements** across 8 Lambda functions. The primary gaps are:

1. **Check handlers** (11-19% coverage) - 539 untested statements
2. **Ingest-docs handler** (40% coverage) - 147 untested statements
3. **Generate-code-maps handler** (47% coverage) - 118 untested statements
4. **Vector-query handler** (66% coverage) - 41 untested statements

## Coverage by Lambda Function

### ✅ Already Meeting 80% Target (Keep these maintained)

| Lambda Function | Coverage | Statements | Missing | Status |
|----------------|----------|------------|---------|--------|
| ask-claude | **96%** | 85 | 3 | ✅ Excellent |
| analyze-pr | **93%** | 172 | 12 | ✅ Excellent |
| process-pr-check | **90%** | 135 | 14 | ✅ Excellent |
| query-kb | **84%** | 69 | 11 | ✅ Good |
| lambda_backend | **81%** | 197 | 37 | ✅ Good |
| process-batch-summary | **80%** | 140 | 28 | ✅ At target |

**Supporting modules at 100%:**
- generate-code-maps/backends/factory.py
- generate-code-maps/state_tracker.py
- process-pr-check/check_handlers/__init__.py

### ⚠️ Below 80% Target (Needs work)

| Lambda Function | Coverage | Statements | Missing | Priority |
|----------------|----------|------------|---------|----------|
| vector-query | 66% | 119 | 41 | High |
| backends/__init__ | 50% | 12 | 6 | Medium |
| generate-code-maps | 47% | 221 | 118 | High |
| ingest-docs | 40% | 246 | 147 | High |
| backends/base | 90% | 40 | 4 | Low |

### ❌ Critical Gaps (Very low coverage)

| Check Handler | Coverage | Statements | Missing | Priority |
|--------------|----------|------------|---------|----------|
| adr_compliance | 11% | 182 | 162 | Critical |
| architectural_duplication | 13% | 143 | 125 | Critical |
| breaking_changes | 15% | 144 | 122 | Critical |
| readme_freshness | 13% | 126 | 109 | Critical |
| test_coverage | 19% | 26 | 21 | Critical |

**Total missing in check handlers: 539 statements**

## Detailed Gap Analysis

### 1. Check Handlers (11-19% coverage) - CRITICAL

**Files:**
- `lambda/process-pr-check/check_handlers/adr_compliance.py` - 11% (162 missing)
- `lambda/process-pr-check/check_handlers/architectural_duplication.py` - 13% (125 missing)
- `lambda/process-pr-check/check_handlers/breaking_changes.py` - 15% (122 missing)
- `lambda/process-pr-check/check_handlers/readme_freshness.py` - 13% (109 missing)
- `lambda/process-pr-check/check_handlers/test_coverage.py` - 19% (21 missing)

**What's missing:**
- Handler initialization and configuration loading
- GitHub API interaction logic
- AI prompt construction for Claude analysis
- Response parsing and validation
- Error handling for Bedrock throttling
- Edge cases (empty PR diffs, no ADRs found, etc.)

**Why critical:**
- These contain core business logic for PR checks
- Most complex part of the system (AI-powered analysis)
- High risk of bugs in production
- Most visible to users (PR comments)

**Test effort:** ~50-60 new tests needed

---

### 2. Ingest-Docs Handler (40% coverage) - HIGH PRIORITY

**File:** `lambda/ingest-docs/handler.py` - 40% (147 missing)

**Missing lines:** 55-71, 97-99, 114-126, 141-161, 192-225, 264-268, 415, 420-421, 445-467, 476-588

**What's missing:**
- `load_config()` - SSM parameter loading (lines 55-71)
- `github_api_request()` - GitHub API calls (lines 97-99, 114-126)
- `ingest_adr()` - ADR ingestion logic (lines 141-161)
- `ingest_readme()` - README ingestion logic (lines 192-225)
- `ingest_doc()` - Generic doc ingestion (lines 264-268)
- Error handling for S3/DynamoDB failures
- Handler orchestration logic (lines 476-588)

**Why high priority:**
- Critical for knowledge base functionality
- Errors here break the entire RAG system
- Complex AWS service interactions (S3, DynamoDB, Bedrock)

**Test effort:** ~20-25 new tests needed

---

### 3. Generate-Code-Maps Handler (47% coverage) - HIGH PRIORITY

**File:** `lambda/generate-code-maps/handler.py` - 47% (118 missing)

**Missing lines:** 81-121, 135-149, 163-173, 217, 290, 340-342, 422-451, 465-614

**What's missing:**
- `load_config()` - Configuration loading (lines 81-121)
- `generate_architectural_summary()` - AI summary generation (lines 135-149)
- `generate_embedding()` - Bedrock embedding calls (lines 163-173)
- Handler orchestration and batch processing (lines 422-451, 465-614)
- Error handling for Bedrock errors
- S3/DynamoDB error paths

**Why high priority:**
- Powers code understanding for the assistant
- Complex backend abstraction logic
- High cyclomatic complexity

**Test effort:** ~18-22 new tests needed

---

### 4. Vector-Query Handler (66% coverage) - MEDIUM PRIORITY

**File:** `lambda/vector-query/handler.py` - 66% (41 missing)

**Missing lines:** 44-51, 88-90, 127-167, 231, 236-239, 270, 289-290, 298-299, 313-315

**What's missing:**
- `load_config()` - SSM configuration (lines 44-51)
- Error handling in `generate_embedding()` (lines 88-90)
- `scan_documents()` - DynamoDB pagination logic (lines 127-167)
- Error handling in `search_documents()` and `handler()` (scattered)

**Why medium priority:**
- Already at 66% (closer to target)
- Core search functionality is tested
- Missing mostly error paths and edge cases

**Test effort:** ~8-10 new tests needed

---

### 5. Lambda Backend (81% coverage) - LOW PRIORITY

**File:** `lambda/generate-code-maps/backends/lambda_backend.py` - 81% (37 missing)

**Missing lines:** 24, 125, 134, 139, 144, 149, 154, 242, 263-266, 294, 312, 331, 348, 366, 446-461, 465-481

**What's missing:**
- Error handling in discovery methods
- Edge cases in batch metadata generation
- Some storage key generation paths

**Why low priority:**
- Already above 80% target
- Missing mostly edge cases
- Well-tested core functionality

**Test effort:** ~5-8 new tests needed (optional)

---

## Common Patterns in Missing Coverage

Across all Lambda functions, the following patterns are consistently undertested:

### 1. **Configuration Loading** (load_config functions)
- SSM parameter retrieval failures
- Missing parameters
- Invalid parameter values
- Network errors during startup

### 2. **AWS Service Error Handling**
- DynamoDB ClientErrors (throttling, service exceptions)
- S3 upload/download failures
- Bedrock throttling and service errors
- SQS send failures

### 3. **Error Recovery Paths**
- Retry logic for transient failures
- Graceful degradation when dependencies fail
- Logging of errors with context

### 4. **Edge Cases**
- Empty responses from APIs
- Null/undefined values in data structures
- Boundary conditions (empty lists, max limits)
- Invalid data formats

### 5. **Handler Orchestration**
- Event parsing failures
- Missing required fields in events
- Complex conditional flows
- Exception handling at handler level

## Impact Analysis

### To reach 80% coverage, we need:

**Total additional statements to test: 540**

Breakdown by priority:
- **Critical (check handlers):** 539 statements → 50-60 tests
- **High (ingest-docs):** 147 statements → 20-25 tests
- **High (generate-code-maps):** 118 statements → 18-22 tests
- **Medium (vector-query):** 41 statements → 8-10 tests

**Estimated total effort:** 96-117 new tests

**Time estimate:**
- Critical priority: 2-3 days
- High priority: 2-3 days
- Medium priority: 1 day
- **Total: 5-7 days** of focused test writing

## Recommendations

### Phase 1: Critical Gaps (Target: 65% coverage)
Focus on check handlers to get biggest coverage boost:

1. **test_coverage.py handler** (smallest, easiest win)
   - 26 statements, 21 missing
   - ~5-6 tests needed

2. **adr_compliance.py handler**
   - 182 statements, 162 missing
   - ~15-18 tests needed

3. **readme_freshness.py handler**
   - 126 statements, 109 missing
   - ~12-14 tests needed

**Expected result:** +15% coverage (55% → 70%)

### Phase 2: High Priority (Target: 75% coverage)

4. **ingest-docs/handler.py**
   - Focus on happy paths and AWS error handling
   - 20-25 tests

5. **vector-query/handler.py**
   - Fill error handling gaps
   - 8-10 tests

**Expected result:** +10% coverage (70% → 80%)

### Phase 3: Remaining Handlers (Target: 80%+)

6. **architectural_duplication.py**
7. **breaking_changes.py**
8. **generate-code-maps/handler.py** (if time permits)

**Expected result:** +5-10% coverage (80% → 85-90%)

## Next Steps

1. ✅ **Coverage audit complete** (this document)
2. **Create test improvement plan** - Prioritized list of specific tests to write
3. **Write tests** - Systematic implementation following ADR-005 standards
4. **Monitor coverage** - Track progress toward 80% goal
5. **Enforce in CI** - Add coverage gate to fail builds below 80%

---

**Generated:** 2025-01-06
**Next audit:** After Phase 1 completion (check handlers tested)
