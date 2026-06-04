# Backlog

Tracked low/medium-priority work that hasn't shipped yet. Not a roadmap
(see Jira); this is the engineering-internal "we noticed it, didn't fix
it now, don't lose it" file.

## Open

### Build-everything target discoverability

**Filed:** 2026-06-04
**Severity:** Low
**Component:** Makefile / docs

`make pre-deploy` already does what every operator instinctively
reaches for — runs `build-all-layers` + `build-all-images` and
transitively builds every layer (runtime, platform-ca, terraform-tools)
and every container image (run-tests, ui, mcp-server, snyk, database).
Idempotent end-to-end. As of 2026-06-04 the build-terraform-layer
gap is closed (added to `scripts/build-all-layers.sh`).

**Remaining ergonomics concern:** `pre-deploy` is the wrong name.
It sounds like a CI step, not "do every build at once before terraform
apply." Operators don't discover it from `make help` because the
verbal cue isn't there, so they reach for 5-7 separate `make
build-{thing}` invocations and frequently miss one.

**Fix shape:**
1. Add an alias target: `build-all` or `build-everything` defined as
   `build-all: pre-deploy`. Keep `pre-deploy` as the canonical name
   for docs / muscle memory; the alias is just for discoverability.
2. Update `make help` to advertise the alias prominently in the
   "Bootstrap a workspace" section (currently `make help` doesn't
   call out `pre-deploy` at all).
3. Update `docs/getting-started.md` and any per-environment quickstart
   to lead with the new target.
4. Optional: post-build verification step that prints ECR URIs +
   SHA256 digests so the operator can confirm push success without
   greping terraform output.

**Not blocking:** `ENVIRONMENT=<env> make pre-deploy` works correctly
end-to-end for any workspace including the openai one. This is
discoverability + ergonomics, not correctness.

---

## Closed (kept for historical context until pruned)

_(none yet)_
