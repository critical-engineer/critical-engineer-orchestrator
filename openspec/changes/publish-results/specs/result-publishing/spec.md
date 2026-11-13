# Spec Delta

## Purpose
Make a completed plan's results reviewable outside the box, with their evidence, without exposing credentials or ever touching the protected branch.

## ADDED Requirements

### Requirement: Publish a completed plan for review
When every task of an approved plan is completed, CEO SHALL push the task branches and open one pull request for the plan against the protected branch, so the engineer can review and merge it.

#### Scenario: Plan completed
- **WHEN** every non-removed task of an approved plan is `completed`
- **THEN** the plan's integration branch `ceo/<change>/integration` (the approved commit plus every completed task, in the order they completed) is pushed to the plan repo
- **AND** one pull request from that branch to `main` is opened

#### Scenario: Published once
- **WHEN** a plan has already been published for its current approved version
- **THEN** a later cycle does not push or open anything again

#### Scenario: Publishing failed
- **WHEN** publishing a completed plan fails, whether the push or the pull request call
- **THEN** a later cycle tries again, even though the plan's completion was already announced
- **AND** a push that already succeeded is not rejected when it's retried, even if CEO stopped before recording it
- **AND** if the remote branch holds a commit CEO never pushed, CEO doesn't overwrite or adopt it, and asks the engineer

#### Scenario: Pull request already exists
- **WHEN** a pull request from `ceo/<change>/integration` already exists
- **THEN** CEO updates its branch and description instead of opening a second one

### Requirement: Evidence in the pull request
The pull request description SHALL let the engineer judge the result without visiting the box.

#### Scenario: Description contents
- **WHEN** the pull request is opened or updated
- **THEN** its description lists each task with its commit, the check that was run and its exit code, the model used and the cost
- **AND** it lists every judgment gate for the plan with its decision, who decided it and by which route
- **AND** it states the plan's approved commit

### Requirement: The protected branch is never written
CEO SHALL only ever push branches under `ceo/`.

#### Scenario: Attempt to push elsewhere
- **WHEN** a push to any branch outside `ceo/` (including `main`) is requested
- **THEN** it is refused before anything is sent, and the refusal is recorded

### Requirement: Credentials stay secret
The publishing token SHALL never appear in a command line, a URL, git configuration, the ledger, chat messages or logs, and SHALL never be available to a worker.

#### Scenario: Pushing with the token
- **WHEN** CEO pushes a branch
- **THEN** the token reaches git only through the environment of that one git process, and no command-line argument, remote URL or configuration file contains it

#### Scenario: Errors
- **WHEN** a push or an API call fails
- **THEN** the recorded error and any chat message do not contain the token

#### Scenario: Workers not isolated
- **WHEN** the GitHub publisher is configured but workers don't run as a separate user
- **THEN** CEO refuses to start with a clear error, because a worker running as CEO could read the token

### Requirement: Pluggable publisher
Publishing SHALL go through one interface, chosen by configuration, with "none" (keep results on the box) as a valid choice.

#### Scenario: No publisher configured
- **WHEN** the publisher setting is `none` or absent
- **THEN** a completed plan is announced as today and nothing is pushed
- **AND** if a real publisher is configured later, plans completed in the meantime are published then

#### Scenario: Unknown publisher
- **WHEN** the publisher setting names an unknown kind
- **THEN** CEO refuses to start with a clear error
