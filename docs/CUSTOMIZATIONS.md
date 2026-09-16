# Repository customizations

This deployment follows `origin/dev` and intentionally keeps only three local
operational extensions:

1. **Blue-green deployment**
   - `deploy/blue-green-deploy.sh`
   - `deploy/BLUE_GREEN.md`
   - `deploy/tests/blue-green-deploy-test.sh`
2. **Automatic low-balance recharge**
   - `deploy/sub2api-auto-recharge.py`
   - `deploy/sub2api-auto-recharge.service`
   - `deploy/sub2api-auto-recharge.timer`
   - `deploy/tests/test-auto-recharge-runtime.py`
3. **Cockpit token synchronization**
   - `tools/cockpit-token-sync/`
   - the minimal account/quota service integration required by that syncer

Everything else should continue to follow upstream. In particular, image model
routing, model defaults, retry policy, migrations, API behavior, and normal CI
workflows are upstream-owned. The production image currently uses upstream's
`gpt-5.6-luna` Responses driver for the `gpt-image-2` image tool, with
`SUB2API_IMAGES_MAIN_MODEL` available as the upstream-provided override.

The following are deliberately not part of this customization layer:

- the former retry-count change from 5 to 10;
- prompt or response capture;
- account-specific operational skills;
- a repository-scheduled production auto-deploy workflow.

## Updating from upstream

1. Fetch and integrate the latest `origin/dev` in an isolated worktree.
2. Resolve conflicts by preserving only the three extensions above.
3. Run the blue-green, recharge, Cockpit sync, and relevant Go test suites.
4. Build a `linux/amd64` image in CI or on a separate build host.
5. Back up production, then deploy the prebuilt image with the blue-green
   script and verify public health plus a real feature smoke test.

Never build source code or container images on the Los Angeles production
server. A Codex heartbeat may periodically inspect upstream and recommend an
update, but it must not deploy without explicit confirmation.
