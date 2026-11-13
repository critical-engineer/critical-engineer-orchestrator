# Tasks

## 1. Tests first (the engineer signs these off before any implementation)

- [ ] 1.1 Write tests/test_publish.py covering every scenario in specs/result-publishing/spec.md and every failure mode F1-F7 in design.md, against a module ceo/publish.py that doesn't exist yet: use a local bare git repository as the remote (see tests/test_approval.py for the pattern) and tests/fake_http.py's FakeHttpApi as GitHub's API; assert the token never appears in any command line (record the argv of every git call), remote URL, git config, ledger event or error message; assert pushes outside ceo/ are refused before git runs; assert the PR description lists each task's commit, check, exit code, model, cost and every gate with decision, decider and route; cover the GitHub publisher refusing to start without a separate worker user (C8) and a retry after the push succeeded but the PR call failed (F8, D5); at least 12 test functions, and don't create ceo/publish.py

## 2. Implementation

- [ ] 2.1 Implement ceo/publish.py so that tests/test_publish.py passes without changing any test: a Publisher interface (D1) with NonePublisher and GitHubPublisher, a function that picks one from CEO_PUBLISHER (none or github; anything else raises; github without a CEO_WORKER_USER raises, C8), the PR description built only from the ledger (D2), git push through a GIT_ASKPASS helper with the token only in that git process's environment (D4), publish.pushed recorded after every successful push and --force-with-lease against the last pushed commit (D5), and GitHub calls only through ceo.notify.open_url (C5)

## 3. Wiring

- [ ] 3.1 In ceo/run.py, publish every completed plan once per approved commit as its own step, checked by its own publish.done record and not skipped because the completion was already announced (D3): record publish.done with the approved commit and the URL, or publish.failed with the error (without the token) and retry on the next cycle; a publishing failure must not fail the plan or stop other plans; add tests for this to tests/test_run.py

## 4. Settings and docs

- [ ] 4.1 Document CEO_PUBLISHER, CEO_GITHUB_PUSH_TOKEN and CEO_GITHUB_BOT in docs/hosting.md (including the exact token permissions: this repository only, contents and pull requests read/write) and add them, empty, to the settings file that scripts/setup-box.sh writes
