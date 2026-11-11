*Issue #, if available:*

*Description of changes:*

*Checklist* (see [CONTRIBUTING.md](https://github.com/awslabs/social-messaging-agent-runtime-toolkit/blob/main/CONTRIBUTING.md); explain in the description anything that applies to your change but is not ticked):

- [ ] `make check` passes (ruff lint, ruff format check, and the non-slow test suite, including the layering rules)
- [ ] `bash scripts/check_license_headers.sh` passes
- [ ] New behavior has tests, and no existing test was weakened to make it pass
- [ ] No new environment reads outside the config classes
- [ ] If AWS infrastructure changed: the Terraform is in this pull request, and `terraform fmt -check -recursive` and `make tf-validate` pass
- [ ] If `pyproject.toml`, `src/socialagent/cli/_assets.py`, `blueprints/`, `recipes/` or `packages/socialagent-runtime/` changed: `make test-packaging` passes, and for `packages/socialagent-runtime/` the description says how existing projects get the change (the release their `requirements.txt` pins, which `socialagent runtime update` moves)
- [ ] If the admin console changed: `make backend-test` and/or `make ui-test`, `make ui-build` and `make ui-smoke` pass
- [ ] Docs match the change (`AGENTS.md`, `docs/`, `README.md`, and the three blueprint copies of `docs/building-with-socialagent.md`), or the description says what is left stale

By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.
