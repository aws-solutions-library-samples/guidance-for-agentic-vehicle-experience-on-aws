# Publishing CVX to the public GitHub mirror

Target: `aws-solutions-library-samples/guidance-for-connected-vehicle-experience-on-aws`

> **CVX has never been published.** The mirror repo now exists
> (`8d7e6a7 "initial base structure"`, created 2026-08-04) but is **private** — an
> unauthenticated `GET` on the GitHub API returns 404 while SSH with a credentialed key
> reaches it. Nothing is publicly visible yet.
>
> That private window is worth using: a push while the repo is private is **reversible** —
> force-push corrections freely, nothing has ever been public. The **one-way door is the
> flip to public**, not the first push. Per `~/.kiro/steering/secrets-handling.md`, once
> content is public it is public permanently, and an AWS account ID has no revocation path.
> Read this whole page before triggering the job.

## Current state: the gate is CLEAN

As of 2026-08-06 the scanner reports **0 critical findings** against the tree a publish
would produce. This is the first time that has been true.

**Do not trust this sentence — run the gate.** It is the only authority, and the count
moved several times a day while the blockers were being closed:

```bash
bash scripts/lib/verify-publish-safety.sh          # committed HEAD (what CI checks)
bash scripts/lib/verify-publish-safety.sh -w       # working tree, for verifying a fix
```

Four blockers were closed on 2026-08-05/06, each with a `summary.md` in `issues/`:

| Blocker | Count | Resolution |
|---|---|---|
| `infrastructure/bin/vsa.ts` | 12 | STAGE_DEFAULTS parameterization — spec `2026-08-05-cvx-stage-defaults-parameterization`. See `issues/2026-08-04-vsa-ts-ships-real-arns-unscanned/` |
| `agents/supervisor/{config,supervisor}.py` | 6 | Brand canary surviving in comments citing renamed spec slugs. See `issues/2026-08-05-cvx-brand-canary-in-shipping-source-comments/` |
| `docs/DEPLOYMENT.md` | 3 + 5 | Three account IDs, plus five infrastructure coordinates **no scanner pattern can see** (two KMS key UUIDs, a cache endpoint, a VPC id, an SG id). See `issues/2026-08-05-cvx-deployment-runbook-ships-account-ids/` |
| vendored publish-toolkit | — | Drift from the sync anchor, which hard-aborts at Step 0. See `issues/2026-08-05-cvx-publish-toolkit-drift-blocks-publish/` |

Two lessons from that set are worth carrying, because neither is caught by the gate:

**The scanner cannot see every identifier class.** It guards account IDs, Cognito pool IDs,
key material and named canaries. It has no pattern for a bare KMS key UUID, a VPC id, an SG
id or an ElastiCache hostname — which is exactly how five survived in `docs/DEPLOYMENT.md`
with a placeholder account ID beside them making the rows *look* sanitised. A clean gate is
necessary and not sufficient; read the staged tree before the first publish.

**A half-scrubbed value can be reassembled from a second file.** The staging KMS key UUID in
`docs/DEPLOYMENT.md` was the same UUID as in `bin/vsa.ts`, and `vsa.ts` carried the real
account ID. Both shipped. Neither file leaked a full ARN on its own.

**This file tripped its own gate on first commit** (`46496fb`) by naming a canary literal
while explaining it, and had to be corrected. That was the fourth instance in one day of a
guard, or a doc about a guard, embedding the very value it guards — the others being CMS's
`.gitignore`, the canonical toolkit test's docstring, and CMS's `scan_exclude_baseline.py`
comment. If you are writing about a canary, reference it; do not quote it.

## How the gate is enforced

Three layers, deliberately redundant, because the failure mode is irreversible:

1. **`verify_publish_archive_clean`** (verify stage) — builds the archive a publish would
   produce, applies `.publish-exclude`, scans what survives. Currently `allow_failure:
   true` so the known findings above don't block unrelated work.
2. **The publish job's own pre-check** — re-runs the same gate as a hard precondition
   before any remote is touched. This exists because **`allow_failure: true` jobs do not
   block later stages in GitLab**, so stage ordering alone would have allowed a publish
   with a red probe. Invoked with no allowance flags, so unlike layer 3 it cannot be
   waived.
3. **`publish-to-github.sh`** — scans the staged tree and aborts on a non-zero exit. This
   one accepts `--allow-finding`, so it *is* waivable; that is exactly why layer 2 exists.

**When the findings clear, remove `allow_failure` from `verify_publish_archive_clean`** so
the probe becomes blocking in its own right.

## One-time setup

- **`GITHUB_TOKEN`** as a **Protected + Masked** CI/CD variable in the GitLab project
  (`Settings → CI/CD → Variables`). Personal Access Token or fine-grained token with
  `repo` scope on the public repo. The job's rules include
  `if: '$GITHUB_TOKEN == null' → when: never`, so **without it the job does not appear at
  all** — that is the intended safe default, not a misconfiguration.
- Tokens expire. Rotate per organizational policy; a silently-expired token shows up as a
  publish that cannot run.

### Local push guard

Each clone should have the `github` remote's **push** URL set to `no-direct-push`, so an
accidental `git push github main` cannot reach the mirror:

```bash
git remote set-url --push github no-direct-push
git remote get-url --push github          # -> no-direct-push
git push github main                      # -> fatal, exit 128
```

Fetch is deliberately left intact — the post-publish verification step needs it.

`publish-to-github.sh` understands this value natively: `EFFECTIVE_PUSH_URL` resolves
`GITHUB_PUSH_URL_OVERRIDE` (set by CI to an HTTPS+token URL) → the local push URL *unless*
it is `no-direct-push` → the `GITHUB_REMOTE_URL` constant in
`scripts/lib/publish-config.sh`. So the guard costs nothing operationally.

**Caveat: this is per-clone git config, not committed.** A fresh clone has an unguarded
push URL. Re-apply it after cloning. Same tradeoff as `.git/info/exclude` — the protection
cannot be shared without putting it in a tracked file.

## Publish procedure

1. Working tree clean, on `main`, pushed to GitLab origin.
2. **Run the gate and get a clean result.** Not "only known findings" — clean.
3. Annotated semver tag: `git tag -a vX.Y.Z -m "..."` then `git push origin vX.Y.Z`.
4. Local dry-run: `bash scripts/publish-to-github.sh --tag vX.Y.Z --dry-run` — scanner
   must report clean.
5. In GitLab Pipelines, find the tag's pipeline and trigger the **manual**
   `publish_to_github` job. It only appears for tags matching `^v\d+\.\d+\.\d+$`.
6. Verify on the mirror:
   ```bash
   git fetch github && git log --oneline github/main -1
   git ls-remote --tags github
   ```
7. **Check CodeQL Default Setup completes** within ~10 minutes. The
   `aws-solutions-library-samples` org enables it and repo admins cannot disable it. It
   needs at least one workflow file in `.github/workflows/` to build an analysis dataset —
   if it errors with "could not process any of it", confirm a non-CodeQL workflow shipped.
   Do **not** ship an Advanced CodeQL workflow; it conflicts with Default Setup at SARIF
   upload time. See `~/.kiro/steering/public-mirror-publish.md`.

## Notes

- Each publish is a **single squashed commit, force-pushed**. Anything added on the GitHub
  side — Dependabot PRs, manual edits — is wiped by the next release.
- The audit log lands in `.publish-history/<tag>.log` and is retained as a CI artifact for
  one year.
- `.publish-exclude` controls what **ships**; `.publish-secrets-scan.yml` `scan_exclude`
  controls what the scanner **reads**. They are not interchangeable, and a file in the
  second but not the first publishes *unexamined*. That confusion put a real AWS account ID
  on the sibling CMS repo's public mirror twice. `scripts/lib/test_publish_scan_exclude_invariant.py`
  now enforces the invariant; do not resolve a scanner finding by widening `scan_exclude`.
