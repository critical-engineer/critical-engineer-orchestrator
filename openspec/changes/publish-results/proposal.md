# Proposal

## Why

CEO commits each task's result on a `ceo/<change>/<task>` branch in its own clone on the box.
Nothing leaves the box, so the engineer can't review the work without SSH, and approval of
results has no durable home. This is the first plan CEO builds for itself (Phase 3, dogfood):
small, testable, and the gap it closes is the one between "the check passed" and "the
engineer can review and merge it".

## What Changes

- When every task of an approved plan is completed, CEO publishes the result for review: it
  pushes the task branches and opens (or updates) one pull request for the plan, with the
  evidence (checks, costs, gates) in the description.
- Publishing goes through a small publisher interface with one implementation for GitHub, so a
  non-GitHub host can be added later without touching the loop. "No publisher" stays possible.
- CEO writes to GitHub as a separate bot account with a token limited to the plan repo. The
  bot can open PRs but never approves anything (C10 of the bootstrap plan).

## Capabilities

### New Capabilities
- `result-publishing`: making a completed plan's results reviewable outside the box, with their evidence, without exposing credentials or touching `main`.

### Modified Capabilities

## Impact

- New module for publishing; the orchestrator loop calls it once per completed plan.
- New settings: the publisher kind and the bot's token, in `/etc/ceo/env`.
- No new dependency: GitHub's REST API over the standard library, `git push` over HTTPS.
