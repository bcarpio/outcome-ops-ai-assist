# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Scheduled GitHub sync self-heal knobs
#
# The hourly scheduled sync repairs a commit whose pipelines (docs, code
# maps, symbol graph) were dispatched but never completed. These two
# variables bound it: a per-target attempt budget in github-sync and a
# per-run slot budget in github-scheduler. Kept in their own file because
# variables.tf is already past the code-map embedding size limit.
# ============================================================================

variable "github_sync_heal_max_attempts" {
  description = "Scheduled self-heal: max automatic attempts to complete one stalled commit before the repo is marked stalled. 0 disables healing in github-sync."
  type        = number
  default     = 3
}

variable "github_sync_heal_max_per_run" {
  description = "Scheduled self-heal: max repos github-scheduler lets heal per hourly run (oldest first). 0 disables healing."
  type        = number
  default     = 20
}
