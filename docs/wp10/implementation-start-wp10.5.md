# WP10.5 Implementation Start

Date: 2026-08-31 (Asia/Shanghai)
Role: implementer (not gate signer)
Worktree: `/private/tmp/uap-wp10.5-impl`
Branch: `codex/wp10.5` (no upstream)

## Gate accepted

- Gate: `G10-GATE-10.4`
- Signed SHA: `19ee4f436179740c002f05f597e456d42b1a17db`
- Independent review: PASS
- Report: `/tmp/uap-wp10.4-codex-review.md`
- Authorization: enter WP10.5 only, from the signed first-parent SHA above

## Start-of-phase identity

- HEAD equals signed SHA `19ee4f436179740c002f05f597e456d42b1a17db`
- First-parent chain: `10773f1` → `ec06ab8` → `19932ae` → `15b85ed` → `19ee4f4`
- Worktree clean
- No upstream

## Authorized scope

Map frozen WP9/WP10 ops wrappers to an independent OIDC Admin API. Deliver G10-21–G10-24 and all 66 W01–W11 matrix instances. No new domain capability. No standalone grant write route.

## Forbidden

- Modify migrations 0001–0023
- Modify signed WP1–WP9 or WP10.1–WP10.4 frozen contracts
- Standalone grant write route
- Admin handler direct INSERT/UPDATE/DELETE
- Call private `_apply_*`, manifest capture, outbox enqueue
- Call `core.merge_entities` or `core.reverse_entity_merge`
- Generic sources/jobs/prompts/roles/audit management APIs
- Cookie session or CSRF cookie auth
- Bulk API, GraphQL, WebSocket, SSE
- Relation DTO, success route, projection, or materializer
- External/semantic search
- WP10.6, WP11
- Makefile, Docker/compose, workflow, or CI wiring
- Push, create PR, start CI
- Amend, rebase, merge, squash, force-push
- Modify or clean the main worktree

## Stop line

After one ordinary linear append commit: STOP. Do not sign `G10-GATE-10.5`. Do not enter WP10.6. Do not push, create a PR, or start CI.

## Main worktree (read-only)

- Path: `/Users/shonchun/Documents/UAP平台`
- Branch: `codex/entity-normalization`
- HEAD: `4f09b0aad0b13c94b6435e4205fa02c046de46e5`
- Tracked index fingerprint: `0279ee036f8bf5893d446d542d602f22f1b6c0e320478e0b84814e5df68d5ec1`
- Start-of-phase tracked diff (`git diff HEAD | shasum -a 256`): `e651e4f4816ddc9cf725db41fe2c5af88d36a2229bfddbffa7647c89215b9b4c`
- Start-of-phase untracked path (`git ls-files --others --exclude-standard | LC_ALL=C sort | shasum -a 256`): `9f20514f26da889bab489adab9474da3c285e08861ca7ff3fb5346f691da0b67`
- Untracked content fingerprint: `fed590956d05a7fae1a675ea6cc4914f22b0eb4925460b57b23b990db90c4ffe`
- Pre-existing dirty set left untouched: `M data/uap.db` plus the listed untracked WP0 artifacts

## REJECT remediation (2026-09-01)

Independent review REJECTED candidate `b5ad271` (report `/tmp/uap-wp10.5-codex-review.md`).
This worktree remediates P1-1 cursor env, P1-2 W-matrix before/after semantics, P1-3 W04 concurrent revise, P1-4 runtime RSA, P2-1 trailing whitespace, and P2-2 ephemeral helpers moved out of the worktree.
Still not signing `G10-GATE-10.5`. Still not entering WP10.6.


## Second REJECT remediation (2026-09-01)

Independent review REJECTED candidate `1ea9ae0` solely because W04 same-request replay asserted historical `publication.grant_id` / `publication.revision` (thread-0 winner only).
This worktree remediates that probe assertion: replay both concurrent revise requests, assert HTTP 200 + same `resource_id` + zero grant/manifest/outbox delta, and treat publication as the live active grant. Production Admin API behavior is unchanged.
Still not signing `G10-GATE-10.5`. Still not entering WP10.6.
