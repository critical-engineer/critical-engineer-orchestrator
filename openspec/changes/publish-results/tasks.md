# Tasks

## 1. Tests first (the engineer signs these off before any implementation)

- [ ] 1.1 Write tests/test_publish.py covering every scenario in specs/result-publishing/spec.md and every failure mode F1-F7 in design.md, against a module ceo/publish.py that doesn't exist yet: use a local bare git repository as the remote (see tests/test_approval.py for the pattern) and tests/fake_http.py's FakeHttpApi as GitHub's API; assert the token never appears in any command line (record the argv of every git call), remote URL, git config, ledger event or error message; assert pushes outside ceo/ are refused before git runs; assert the PR description lists each task's commit, check, exit code, model, cost and every gate with decision, decider and route; cover refusing to start when a token is set or the GitHub publisher is selected and workers aren't a separate user with a different UID, including CEO_PUBLISHER=none with a token, and starting with none, no token and no worker user (C8) and a retry after the push succeeded but the PR call failed (F8, D5), a push that succeeded before CEO could record it (F9), the same after the plan was re-approved at a new commit before CEO recovered (the remote tip matches an unrecorded publish.intent, so it's recorded as pushed and used as the lease target), an unknown commit on the remote branch (no push, publish.failed), and the none publisher recording publish.skipped so that configuring GitHub later still publishes (D3); at least 12 test functions, and don't create ceo/publish.py

## 2. Implementation

- [ ] 2.1 Implement ceo/publish.py so that tests/test_publish.py passes without changing any test: a Publisher interface (D1) with NonePublisher and GitHubPublisher, a function that picks one from CEO_PUBLISHER (none or github; anything else raises; a token or github without a CEO_WORKER_USER whose UID differs from CEO's raises, C8), the PR description built only from the ledger (D2), git push through a GIT_ASKPASS helper with the token only in that git process's environment (D4), publish.intent recorded before and publish.pushed after every push, the remote tip checked against both before pushing, and --force-with-lease against the last pushed commit (D5), and GitHub calls only through ceo.notify.open_url (C5)

## 3. Wiring

- [ ] 3.1 In ceo/run.py, publish every completed plan once per approved commit as its own step, checked by its own publish.done record and not skipped because the completion was already announced (D3): record publish.done with the approved commit and the URL when a real publisher published, publish.skipped for the none publisher (which doesn't stop a later real publisher), or publish.failed with the error (without the token) and retry on the next cycle; a publishing failure must not fail the plan or stop other plans; add tests for this to tests/test_run.py

## 4. Settings and docs

- [ ] 4.1 Document CEO_PUBLISHER, CEO_GITHUB_PUSH_TOKEN and CEO_GITHUB_BOT in docs/hosting.md (including the exact token permissions: this repository only, contents and pull requests read/write) and add them to the settings file that scripts/setup-box.sh writes, with CEO_PUBLISHER=none (the default, D1) and the token and bot login empty
