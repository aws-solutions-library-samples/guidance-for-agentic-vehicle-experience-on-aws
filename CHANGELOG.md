# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-08-06

First release published to the public mirror. 78 commits since 0.1.0.

### Added

- **Tier 2 autonomous platform-health agent** — scheduled, non-conversational agent that
  reasons over vehicle data with nobody waiting, and writes evidence-carrying artifacts a
  conversational agent reads in single-digit milliseconds. EventBridge Scheduler → Lambda,
  three per-component daily runs (battery, tires, brakes), cross-account Athena into the
  Automotive Data Platform. Artifacts carry `computed_at`, `confidence`, `evidence[]`,
  `agent_version` and `inputs_hash`, so staleness is surfaced rather than hidden and
  re-runs are idempotent.

- **Tier 1 health read surface** — `health_findings()` tool that narrates the Tier 2
  artifact, including its `computed_at`, rather than recomputing inside a conversational
  turn.

- **Zone 2 order backend and anonymous kiosk** — order progression via Step Functions,
  with the order state machine kept deterministic; the agent narrates state, it does not
  decide it.

- **Discover and Acquire journeys** — vehicle shopping guidance, bounded trade-in
  valuation grounded in connected-vehicle data, a usage-grounded upgrade path for the
  consumer persona, and live human-advisor handoff to Amazon Connect from the text
  surface.

- **Generated agent inventory with a drift gate** — `docs/AGENT-INVENTORY.md` is produced
  from source and CI fails when the doc and the code disagree.

- **Vehicle-manufacturing and shopping-guide knowledge-base corpora**, collapsed to
  OEM-neutral generic names.

### Changed

- **Per-stage infrastructure coordinates are no longer baked into the CDK entrypoint.**
  `infrastructure/bin/vsa.ts` resolves them from SSM parameters, environment variables or
  CDK context, and fails closed at synth when a required value is absent. Deployers supply
  their own account's values; see `docs/DEPLOYMENT.md`.

- **The pre-publish scan is now a blocking CI gate** (`verify_publish_archive_clean`),
  not advisory. It builds the archive a publish would produce, applies `.publish-exclude`,
  and scans what survives.

- Vendored publish-toolkit synced to 0.3.4, with a drift check that hard-aborts the
  publish at step 0 if a vendored file was edited in place.

### Fixed

- **Knowledge-base `source_category` defaults pointed at deleted corpora**, so Acquire's
  manufacturing lookup and Discover grounding filtered on a category that no longer
  existed and silently returned nothing.

- Athena read paths exercised against the live data platform rather than stubs, which
  surfaced four defects unit tests could not reach — an unqualified table name, a
  `CAST(array<varchar> AS VARCHAR)` type mismatch, an unreachable component, and
  single-page `GetQueryResults` truncating results.

- Numerous publish-safety fixes ahead of first publication; see `docs/PUBLISHING.md`.

### Known Limitations

- **Tier 2 alarms have no notification target.** Both CloudWatch alarms ship with empty
  `AlarmActions`, so an unattended nightly failure produces a stale artifact that the
  conversational agent keeps narrating with a receding `computed_at`. Wire an operator
  SNS topic before relying on scheduled runs.

- **The Tier 2 scan window is fixed at 25 VINs per run** and does not advance, so it does
  not yet cover large fleets.

- **EVE (voice evaluation pipeline)** remains deferred; the voice agent is functional but
  not covered by automated evaluation.

## [0.1.0] — 2026-07-20

### Added

- **Connected Vehicle Experience (CVX) deployment** — CDK-based reference accelerator
  for fleet driver, consumer, and retail personas. Integrates with the Automotive Data
  Platform (ADP) for vehicle knowledge-base grounding and coordinates with the Connected
  Mobility System (CMS) for fleet operations.
  
- **AgentCore-powered multi-agent supervisor** — orchestrates specialized driver,
  consumer, and retail agent personalities via Bedrock AgentCore runtime (bidi/voice
  and HTTP text). Persona-specific tool routing, safety grounding, and multi-turn
  memory management.

- **Automotive knowledge-base integration** — grounding for diagnostic trouble codes,
  vehicle recommendations, and maintenance guidance. Cross-account Athena queries into
  ADP's data lake with query result caching and timeout safeguards.

- **Voice and text conversation flows** — iOS bidi WebSocket voice session management,
  fallback to HTTP text chat via CMS web UI, simulator integration for testing.

- **Pre-sync secret scanner** — Python 3 CLI validates publish staging tree, blocks
  any critical findings (account IDs, internal hostnames, Cognito identifiers).

- **Sanitizing publish flow** — `scripts/publish-to-github.sh` strips internal-only
  paths via `.publish-exclude` and runs the secret scanner before push.

- **AWS Solutions Library boilerplate** — MIT-0 (MIT No Attribution) LICENSE, NOTICE,
  CONTRIBUTING.md, CODE_OF_CONDUCT.md, minimal GitHub Actions lint workflow.

### Known Limitations

- **EVE (voice evaluation pipeline)** is deferred to a follow-on release. The voice
  agent evaluation harness is incomplete; the deployed voice agent is fully functional
  but not yet integrated with automated test coverage.

- **Nova Sonic cross-region support** — limited to us-west-2 and us-east-1 at this
  release; geographic model inference routing is deferred.

- **Retail agent (customer persona)** — functional in framework; limited demo grounding
  and few evaluation test cases.

- **CDK pollution caveat** — `deployment/cdk.context.json` (gitignored) can pick up
  per-developer values that conflict across stages. If a deploy fails with "No export
  named …" errors, clean the relevant entries from `cdk.context.json` and retry.

### Repository Topology

This release is published from an internal GitLab source-of-truth via a sanitizing
publish flow. The public repository at `aws-solutions-library-samples/guidance-for-connected-vehicle-experience-on-aws`
receives only sanitized, semver-tagged releases. Continuous mirroring is intentionally
NOT used; each release is a deliberate human-gated publish.
