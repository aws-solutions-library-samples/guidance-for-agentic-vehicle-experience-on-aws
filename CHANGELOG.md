# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.2] — 2026-09-28

Patch release. Supersedes v0.3.1, which was tagged but never published — it fixed
two of the three defects below and was replaced before release once the third was
found. No functional change to the platform in either.

### Fixed

- **Two undefined names in type annotations failed the gating lint job.**
  `agents/supervisor/tests/test_deterministic_seams.py` annotated a return type as
  `Path` while importing only `pathlib`, and
  `agents/supervisor/tests/test_tools_unit.py` annotated a local as `Any` with no
  `typing` import. Both modules use `from __future__ import annotations`, so the
  annotations are never evaluated and nothing failed at runtime — which is why no
  test caught them. Fixed to match each module's existing convention. See
  `issues/2026-09-28-flake8-f821-gate-red-on-two-annotation-only-names/`.

- **Contract checksum pin matched the previous revision of the OpenAPI document,
  so `tests/schemas/` failed on a clean checkout.**
  `docs/api/avx-findings-actions.openapi.yaml` gained `GET /findings/me` and its
  `CallerFindingListResponse` schema in v0.3.0, but the frozen digest guarding
  that file against an undeclared change was not rotated to match. The guard was
  correct to fail; the release omitted a step it requires. Both JSON schema pins
  were already correct and are unchanged — the v0.3.0 edit was additive
  documentation of an HTTP route, and no data shape moved. See
  `issues/2026-09-28-openapi-contract-pin-not-rotated/`.

- **The executor mutation battery could not start under CI, so 41 guard checks
  never ran.** `agents/executors/tests/mutation_battery.py` launched its child
  pytest with a hardcoded `.venv/bin/python`. A developer checkout has that
  virtual environment; a CI runner installs dependencies into its system Python
  and has none, so the step failed immediately with `FileNotFoundError` rather
  than evaluating any mutation. It now uses the interpreter that started it
  (`sys.executable`), which leaves the local invocation identical. The battery
  itself was healthy throughout — it reports 41 of 41 caught once it can start.
  The workflow step's name, which said 39 entries, is corrected to 41. See
  `issues/2026-09-28-mutation-battery-hardcodes-venv-path/`.

- **A release-publishing job could not be parsed.** One shell line in the
  internal pipeline configuration was an unquoted YAML scalar containing a colon
  followed by a space, which parses as a mapping rather than a string, so the job
  was rejected before it ran. Quoted, and a new guard
  (`scripts/tests/test_gitlab_ci_wellformed.py`) asserts every job's `script`
  entries are strings. That job is not present on the public mirror; the entry is
  here because the fix ships in this tag. See
  `issues/2026-09-28-gitlab-ci-unquoted-colon-broke-tag-pipeline/`.

## [0.3.1] — 2026-09-28 [superseded, never published]

Tagged internally and replaced by 0.3.2 before publication. Its contents are
listed under 0.3.2.

## [0.3.0] — 2026-09-28

On the public mirror this release follows v0.2.2; v0.2.3 was tagged internally
but never pushed or published, so the public jump is 0.2.2 → 0.3.0.

### Added

- **AVX Spec 1a substrate — end-to-end** (`.kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/`,
  closed 2026-09-25 per spec.md § Close-out). Ships the connective tissue between
  Tier 2 conclusions and Tier 1 delivery: Findings/Actions data model, HTTP
  surface, executor dispatch abstraction, broker Lambda, iOS card feed, and APNs.
  All six steps of the live-verification gate executed against real staging with
  no stubs (`verification-run.md`, `verification-run-steps-4-6.md`).

- **Findings/Actions API contract v2.3** — Cognito-JWT-authenticated routes for
  owner reads (`GET /findings/owner/{owner_id}`, `.../owner-fleet/{owner_id}`),
  per-VIN reads (`.../vehicle/{vin}`), approve
  (`POST /findings/{finding_id}/approve` → creates an Action, dispatches via the
  target executor), Action state transitions (`PATCH /actions/{id}`), and
  dismissal (`POST /actions/{id}/dismiss`). Fleet, dealership, and district
  routes are wired with real handlers; their OpenAPI summaries still carry
  `[STUB]` labels pending a follow-on rotation.

- **Owner reads with fail-closed authorization** — `owner_id` is projected on
  the Finding at write time so owner-scoped reads Query rather than Scan. The
  approve gate compares `caller_sub` to the Finding's `owner_id` with the
  empty-string sentinel rejected explicitly, substring match rejected, and no
  fallback to nested `scope.owner_id` on VIN-scoped Findings. Mutation-verified
  across five classes (M-FG11-8 through M-FG11-12).

- **Approve dispatching to DMS** — the `dms` executor calls the existing
  `POST /api/dms/fleet/repair-orders`, carries `avx_action_id`, forwards the
  caller's Cognito JWT, and consumes the cross-account
  `dms.ro.status_changed` EventBridge event for status transitions. Idempotent
  on retry — DMS-side dedupe live-verified 201/200/409 as part of the
  deploy-order gate.

- **AVX broker Lambda** (`vsa-staging-avx-broker-us-west-2`) — DDB-stream
  fan-out with fail-closed owner-namespace enforcement, per-owner rate limits
  and cardinality cap, per-category consent gating, and a dismissal-feedback
  path that demotes matching Finding-kinds within a 30-day trailing window. No
  agent calls a channel API directly; the broker owns channel selection,
  consent, quiet hours, frequency capping, and priority arbitration. All
  policy surfaces are per-stage SSM configuration (`/vsa/{stage}/avx/broker/…`),
  not code constants.

- **Six executors** ship in v1 — `dms`, `vehicle-command`, `retail`, `none`,
  `chat`, `ios-owner`. Adding a target system is one executor class plus an
  integration test.

- **iOS card feed** in the Meridian Motors Companion app — chronological list
  of open Findings and Actions for the signed-in owner, sourced from
  `/findings/owner/{owner_id}` + `/actions/owner/{owner_id}`. Drill-down
  pre-seeds the chat surface with the Finding's evidence.

- **APNs push, P0-only and consent-gated** — every severity creates an in-app
  card; only severity `p0` triggers an APNs push, and only when the owner has
  consented to the finding's category. The threshold is a single stage-scoped
  SSM key, not a per-executor constant.

- **Tier 2 Agent 2 (Vehicle Health & Service) LLM-activation** — the Tier 2
  Lambda now bundles `strands-agents` via CDK Docker bundling with a pinned
  platform and byte-reproducible artifact. Ends the `tokens_used=0` symptom
  the parent initiative shipped with. The P0 verbatim structural bypass in
  `_enrich_with_llm` is preserved.

- **Standing-claims provisioning** (`.kiro/specs/2026-09-24-avx-standing-claims-provisioning/`,
  closed 2026-09-25). The CMS Cognito pool now carries a `custom:customerId`
  attribute, `vehicle-owner` and `consumer` groups, and
  `AttributesRequireVerificationBeforeUpdate = ["email"]`. New
  `deployment/scripts/assign_standing_claims.py` provisions owner, driver, and
  fleet-manager identities end-to-end. Trust flags for `customerId`,
  `vehicleId`, and `fleetIds` are attested via SSM. Live-verified via
  `LIVE_TESTS=1 … --parts A,B,C,D` on 2026-09-25 after re-converging 7 battery
  Findings.

- **RSA stub dispatch (Phase B)** (`.kiro/specs/2026-09-23-cvx-rsa-stub-dispatch/`,
  closed 2026-09-25). Closes the `/escalate` dead-letterbox: five roadside-dispatch
  requests written to `cms-{stage}-vfo-action-queue` from 2026-06-22 to 2026-09-22
  were never read by any consumer. P0 roadside requests now go to a dedicated
  `vsa-{stage}-rsa-dispatch` table with a stream consumer, a scheduled lifecycle
  advancer (REQUESTED → DISPATCHED → EN_ROUTE → ARRIVED, or FAILED), a
  tenant-scoped `GET /rsa/dispatch/{dispatchId}` status route, and alarms. The
  provider is **simulated** (`VSA_RSA_CLIENT=fake`, `SIM-` references,
  `simulated` on the stored row and in the status response); real mode raises
  on every call. The five
  historical rows are left in place, not migrated. Live-verified 27/27 on
  staging; `docs/DEPLOYMENT.md` § RSA Stub Dispatch has the runbook, a
  tear-down that cannot select the API stack, and a read-only smoke test.
  Closed with two security Warnings carried; both are now fixed (see Security).

### Changed

- **Escalation: recommend, don't act**
  (`.kiro/specs/2026-09-23-cvx-escalation-recommend-not-act/`). The agent no
  longer acts on an escalation decision it is not qualified to make; before
  this, a P3 sales enquiry was one token away from a roadside dispatch.
  - Two escalation types. A safety escalation reads its severity from the
    classifier's verdict in session context, and the model cannot pass one. A
    service escalation (`escalate_for_service`) takes no severity from the model
    and can never request roadside or trigger the P0 callback. It still sends
    `P3` to `/escalate`, because the Amazon Connect contact flow routes on that
    attribute. The retail persona's "use P3 for sales enquiries" workaround is
    removed.
  - On a P0 found by the server's triage, the platform shows the safety
    instruction itself (see Fixed and Known Limitations).
    Requesting roadside and opening a human chat are one tap each on the iOS P0
    card. `escalation.autoOpenHumanChatOnP0` opts a tenant into opening the chat
    without the tap, and `escalation.autoRequestRoadsideOnP0` adds a roadside
    request to that automatic escalation; it has no effect on its own. Both
    default to `false`.
  - Optional P0 outbound callback through Amazon Connect, configured per tenant
    under `escalation.callback`. Before a P0 roadside request or callback,
    `/escalate` requires a platform-recorded P0 for the VIN, checks that the
    request's `vehicleId` is that VIN's vehicle, and checks that the caller is
    the driver assigned to it. Otherwise the chat still opens, with neither
    side effect.
  - Deployed to staging 2026-09-25 (`vsa-staging-api`, bidi runtime v69, text
    runtime v37). Live-verified on iOS: the roadside request and chat handoff
    on 2026-09-26, the P0 callback on 2026-09-25.

- **Public repository renamed** to
  `aws-solutions-library-samples/guidance-for-agentic-vehicle-experience-on-aws`,
  matching the Agentic Vehicle Experience name. GitHub redirects the old URL;
  update existing clones with `git remote set-url`.

### Fixed

- **On a P0 the driver is now told to pull over**
  (`issues/2026-09-25-p0-pull-over-never-reaches-the-driver/`). Nothing in the
  platform sent the pull-over instruction; it existed only as a request to the
  model, and a prompt clause told the model the platform had already said it.
  On an iOS rehearsal the model refused and recited its rules instead. Now the
  platform shows the fault's catalog line (exit the vehicle, call 911, brake
  emergency, or pull over) as the first line in the app on every server triage
  path, and on the first-utterance and booking paths asks the model to say the
  same words (see Known Limitations for the rest). The escalation event
  now follows the drawer mirror, which ends a false "unresponsive" banner after
  a roadside request. Server notes to the model carry a per-session tag the
  driver cannot know, and the driver's name is allowlisted before it is
  spliced into safety text. Deployed to staging 2026-09-25 (bidi v69, text
  v37). Live-verified on iOS 2026-09-26: the line is the first one shown and
  the banner no longer appears. In that run the session began with a typed
  question and the model did not speak the line. Since then, on a P0 the
  model's own `triage` result carries the same line as `sayFirst`, and the
  prompt tells the model to say it word for word before any other tool call;
  personas that are not in the vehicle (advisors, technicians, fleet managers)
  do not get it. The text runtime puts the line at the start of its reply if
  the reply lacks it. Deployed to staging 2026-09-26 (bidi v70, text v38), and
  again 2026-09-28 (bidi v72, text v41). Live-verified on the web chat
  2026-09-28: a typed brake report returns the instruction as the start of the
  reply. On iOS the instruction is shown on every P0 checked, but whether the
  assistant **speaks** it after a typed question has still not been observed —
  see Known Limitations.

- **A confirmed emergency and `P0A80` now get the right safety line**
  (`issues/2026-09-27-p0-safety-lines-p0a80-and-driver-confirmed/`). When the
  driver names an emergency and answers yes, the platform now shows the catalog
  line first, as on the classifier path, and asks the model for the same words:
  fire gets the exit-the-vehicle line, a crash the call-911 line, brake loss
  the brake-emergency line, and steering or visibility loss the stop-the-engine
  line (user decision 2026-09-27). The more protective of that and the active
  faults' lines wins, and a line already shown is never replaced by a weaker
  one. `P0A80`, an EV high-voltage battery thermal event, now gets the
  exit-the-vehicle line instead of "pull over at the next safe location", and
  its chat message says the same. Deployed to staging 2026-09-27 (bidi v71,
  text v39); live-verified on the confirmed path that the line is shown first.
  Whether the assistant then speaks it has not been seen.

- **A driver sees their own vehicle's alerts** (`GET /findings/me`, spec
  `2026-09-25-avx-own-vehicle-findings`). The iOS Alerts tab read a route that
  refuses any identifier but the caller's own account id, which is not the
  vehicle's owner id, so a driver saw nothing. There is now one route a signed-in
  caller can ask without naming a scope: the server works out what they are
  entitled to — their assigned vehicle, the vehicles they own, their fleet,
  dealership or district — reads each, and says which ones it used. A caller
  entitled to nothing gets an empty answer rather than a refusal, so probing
  cannot map what exists. A driver identity is scoped to the one vehicle assigned
  to it, and not to other vehicles sold to the same customer. Documented in
  `docs/identity-and-standing-contract.md` § 3.3 and the OpenAPI contract.

- **Active fault codes become alerts within minutes** (the `diagnostics`
  component of the Tier 2 health agent). The agent's three existing components
  read the analytics lake once a night, which is the wrong cadence for a fault
  the vehicle is reporting now. A fourth component reads the live fault history
  every five minutes and writes an alert per affected vehicle, graded by the same
  severity table the voice assistant's triage uses — so a fault is not "critical"
  in one place and "soon" in another. An alert for a fault that has since cleared
  expires by itself within about half an hour; nothing has to delete it. A fleet
  with no active faults is a healthy silence, not an alarm, so the two
  emptiness alarms the nightly components use are deliberately not wired to it.
  Operationally it is unlike the other three in cadence, source, expiry and
  alarms — `docs/DEPLOYMENT.md` § "The diagnostics component" has the comparison.

- **An alert names the vehicle from the registry, not from the fault row**
  (`issues/2026-09-28-diagnostics-vin-taken-from-dtc-rows/`). The discovery scan
  took the VIN off the first fault row that carried one. Most staging fault rows
  carry a stale VIN or none, so alerts were filed against a VIN no vehicle is
  registered with and no owner or driver could read them, while 54 registered
  vehicles with faults were skipped entirely. The VIN now comes from the vehicle
  registry, and a vehicle is only matched when the registry's own index confirms
  that VIN belongs to exactly that vehicle — so a twin cannot borrow another
  vehicle's owner. An unregistered vehicle is skipped rather than guessed at.

- **Web chat answers a driver about their own vehicle**
  (`issues/2026-09-28-text-runtime-no-vin-resolution-from-vehicleid/`, and
  `issues/2026-09-28-webchat-omits-vin-for-driver-identity/` in the connected
  mobility repository). A driver who reported a brake symptom in the web chat
  was asked for a VIN and given no safety instruction: the page sends a VIN only
  when the chat is opened from a vehicle, and nothing turned the signed-in
  driver into a vehicle. The chat route now takes the vehicle from the driver's
  own verified sign-in claim and reads its VIN itself. A vehicle sent by the
  page that is not the driver's is ignored, and once a chat is bound to a
  vehicle, a VIN the assistant picks up from the page or the conversation cannot
  move it: that vehicle is the one classified, and the one any booking is
  written against. Drivers are the only accounts bound this way; fleet, service
  and kiosk chats are unchanged. Deployed to staging 2026-09-28 (text runtime
  v41, API stack); live-verified the same day — the same message now returns a
  P0 and the exit-the-vehicle instruction, for the driver's own vehicle, whether
  the page sends another vehicle or the message names another VIN.

- **The fire line now tells the driver to call 911**
  (`issues/2026-09-28-b0001-fire-p0-directive-omits-911/`). On a fire
  (`B0001_FIRE`) or an EV battery thermal event (`P0A80`), the line shown at the
  P0 moment said to stop, get out and move fifty feet away, but not to call
  911; the unconditional 911 instruction reached the driver only in the chat
  message after a human handoff. The line now ends "and call nine-one-one now",
  with no condition, and `P0A80`'s chat message matches it. A test fails if any
  code on the exit-the-vehicle line loses the clause or makes it conditional.
  Deployed to staging 2026-09-28 (bidi v72, text v40); live-verified on iOS the
  same day on a vehicle with an active `B0001_FIRE`.

- **A P0 session makes one service booking, and a confirmed emergency books at
  P0** (`issues/2026-09-28-p0-double-booking-auto-book-plus-model-book/`,
  `issues/2026-09-27-driver-confirmed-p0-books-at-classifier-grade/`). The
  platform's automatic P0 booking and the model's own `book` call each wrote a
  service-history row, so every P0 session left two tickets. Booking is now
  idempotent per session and priority, and the model is told the ticket
  exists. When the driver confirms an emergency, the platform now books it at
  P0 instead of the classifier's lower grade; the model still cannot choose a
  priority. Deployed to staging 2026-09-28 (bidi v72, text v40). Live-verified
  on iOS the same day that a classifier P0 session writes one P0 row; the
  confirmed-emergency booking has not yet been run live.

- **The assistant no longer tells a driver a vehicle has no open recalls**
  (`issues/2026-09-25-recall-rule-asserts-no-open-recalls/`). The fleet
  driver's recall rule said to report "no open recalls" whenever retrieval
  found nothing, but there is no recall catalog, so the claim had no data
  behind it. Every recall rule and hard rule 2 of both prompt templates now
  say to report that recall information could not be found and to suggest a
  dealer or nhtsa.gov/recalls with the VIN. `vehicle_identity` no longer
  claims to return recall eligibility. The three recall-status evals now
  reject every listed no-recalls phrasing and accept the fallback, checked by
  a test that scores sample answers the way the eval runners do; paraphrases
  outside the list still pass (see Known Limitations). Deployed to staging
  2026-09-25 (bidi v69, text v37). Live-verified on iOS 2026-09-26: asked about
  recalls, the assistant said it could not confirm them for this vehicle and
  pointed to a dealer and nhtsa.gov/recalls. That session could not reach the
  vehicle knowledge base (fixed the same day, see below), so the answer came
  from fallback documents.

- **Fire, crash and 20 other fault codes are no longer graded "safe to
  drive"** (`issues/2026-09-26-classifier-grades-fire-and-crash-codes-p2/`).
  The triage classifier's severity table had no rows for `B0001_FIRE` or
  `U3000_CRASH`, so both got the default P2 grade while the app told the driver
  to get out or call 911. `B0001_FIRE`, `U3000_CRASH`, `U3000` and `P0520`
  (oil pressure below 15 psi) now classify P0, and 18 airbag, steering, brake,
  engine and lost-module codes classify P1. A parity check, run by the
  classifier build and a CI job, fails if a stop-driving code is not P0 or a
  code is graded below the CMS event catalog's grade;
  `make check-dtc-severity-drift` compares against the live catalog. Deployed
  to staging 2026-09-26; live-verified on a vehicle with an active
  `B0001_FIRE`, which classified P0.

- **The assistant can reach the vehicle knowledge base again**
  (`issues/2026-09-26-core-deploy-dropped-adp-kb-retrieve-grant/`). A
  `vsa-staging-core` deploy on 2026-09-23 without the `adpKbId` context removed
  the runtime role's `bedrock:Retrieve` grant, and `retrieve` fell back to
  local documents. Redeployed with the context on 2026-09-26; the IAM policy
  simulator allows the call.

- **Fire and crash chat diagnoses no longer say emergency services are en
  route** (`issues/2026-09-24-dtc-chat-diagnosis-claims-emergency-services-en-route/`).
  Nothing in the platform calls emergency services. `B0001_FIRE` now tells the
  driver to get clear of the vehicle and call 911. `U3000_CRASH` tells the
  driver to call 911 unconditionally, matching its spoken line, and a test
  stops its written text from making that directive conditional. Deployed to
  staging 2026-09-25 (bidi v68 and v69). Both were live-verified on iOS
  2026-09-28: the fire instruction on a vehicle with an active fire fault (see
  the fire-line entry above), and the crash chat message on a seeded collision
  fault — it tells the driver to call 911 with no condition attached, and says
  nothing about responders being on their way.

- **Tier 2 operator alarms now deliver**
  (`issues/2026-09-24-tier2-operator-alarms-never-delivered/`). Two defects on
  the alerts topics: `enforceSSL: true` was emitting a topic policy whose only
  statement was a `Deny` on non-TLS publish, replacing SNS's default and leaving
  CloudWatch unauthorized to publish; and the topics were encrypted with a key
  CloudWatch could not use. Now a stack-owned customer-managed key
  (`alias/vsa-{stage}-tier2-alerts`) grants `cloudwatch.amazonaws.com`
  `kms:Decrypt` + `kms:GenerateDataKey*` under `aws:SourceAccount`, both topic
  policies carry an explicit CloudWatch `Allow`, and the SSL `Deny` stays. New
  `scripts/live-verify-tier2-alarms.py` (`LIVE_TESTS=1`) forces each alarm to
  ALARM and asserts the newest `Action` history item is `Succeeded`. 9/9 Tier
  2 alarms and 7/7 broker + executor-terminal-failure alarms live-verified
  2026-09-24; the same alarms had failed every action since deploy (2026-09-18
  ×3, 2026-09-23 ×2).

- **Tier 2 deploy no longer rolls back on an em dash**
  (`issues/2026-09-24-tier2-deploy-iam-description-charset/`). IAM validates
  Role `Description` against `[\t\n\r\x20-\x7E\xA1-\xFF]*`; the AVX broker
  role's description contained U+2014 and CloudFormation rolled back the
  create. All IAM role and managed-policy descriptions in
  `vsa-tier2-stack.ts` are now ASCII, guarded by a jest test that fails if the
  em dash is reinserted.

### Security

- **RSA dispatch table on its own customer-managed key, with deletion
  protection** (`issues/2026-09-25-rsa-dispatch-table-on-aws-managed-key/`).
  The table used the AWS-managed `aws/dynamodb` key; it now uses a stack-owned
  key (`alias/vsa-{stage}-rsa-dispatch`, rotation on; the key, not its alias,
  is retained on stack delete), and `deletionProtection` is on. Fixed in `6d307f9` and deployed to
  staging 2026-09-25; the live table reports `KeyManager: CUSTOMER` and
  deletion protection on. Live-verified 2026-09-26: a real roadside request
  was written, advanced and read back through the new key by all four roles
  that use it.

- **`/escalate`'s position-freshness threshold is bounded**
  (`issues/2026-09-25-rsa-position-max-age-accepts-non-finite/`). `nan`,
  `inf` or a very large value turned the check off. The handler now accepts
  only a finite value above 0 and at most 3600 seconds, and refuses every
  dispatch otherwise; the CDK stack and the Makefile guard reject such a value
  at deploy. Fixed in `6d307f9` and deployed to staging 2026-09-25: the
  deployed handler is byte-identical to that commit and reads 900. The bound
  itself is not exercised live, since that would mean deploying a bad value.

- **Fail-closed authorization on every AVX route.** Every route stays
  fail-closed under a missing or malformed claim; each is live-tested with a
  positive and a negative. Approve rejects the empty-string owner-id sentinel
  explicitly and no route falls open on an unresolved caller namespace.

- **Email is no longer a trust primitive on the CMS Cognito pool.** The pool
  setting that let a user rewrite an unverified email and have it become their
  token claim is closed
  (`AttributesRequireVerificationBeforeUpdate = ["email"]`). The identity
  contract already avoided `email` as an identity; the pool now matches.

- **Pool-check script paginates.** `scripts/check-standing-claim-pools.sh`
  no longer passes `--max-results`, so it follows every page of app clients
  rather than checking only the first.

### Known Limitations

- **AVX contract OpenAPI still labels `[STUB]`** on the fleet, dealership, and
  district route summaries in
  `docs/api/avx-findings-actions.openapi.yaml`, even though the handlers now
  return real data. Rotation is a follow-on (backlog: "AVX contract STUB
  labels").

- **Standing-claims spec closed by waiver, not by a review PASS.** No review
  cycle passed on this spec; security review Cycle 1 (PASS) predates Fix
  Groups 3–5. Cycle 5 was accepted by the user without a Cycle 6.

- **The recall regression guards are lists of phrasings.**
  `agents/supervisor/tests/test_recall_rule_no_false_assurance.py` fails if a
  prompt's recall rule tells the model to claim there are no recalls, but some
  paraphrases still pass it (for example "… as there are no open recalls"), and
  a few correct prohibitions are flagged; accepted after six review cycles. The
  recall evals, including the live one, reject a fixed list of no-recalls
  phrasings, so an answer like "nothing outstanding on recalls; check with
  your dealer" still passes them. The rule's required fallback is the other control.

- **Part of a P0 still relies on the assistant's speech.** On every P0 path the
  platform shows the line and the assistant is given its words. In the web chat
  the words are now confirmed to reach the driver. On iOS, whether the assistant
  **speaks** them after a typed question, or after a confirmed emergency, has
  still not been observed; the line is shown either way.

- **A confirmed emergency's assistant-started handoff carries the classifier's
  grade.** When the driver confirms an emergency the classifier graded P2 or
  P3, the service booking is now made at P0, but a human handoff the assistant
  starts itself still carries the lower grade. A handoff the driver taps for
  goes out at P0. If the model booked at the lower grade before the driver
  confirmed, that booking stays alongside the P0 one
  (`issues/2026-09-27-driver-confirmed-p0-books-at-classifier-grade/`).

- **The severity checks compare against the CMS event catalog only.** A
  producer that writes a code more severely than the catalog grades it (found
  for `P0520` and `B1419`) is not caught automatically. That and five other
  fault-code findings are in
  `issues/2026-09-26-dtc-grading-and-copy-follow-ons/`.

- **`U3000` is graded P0 everywhere.** The one staging vehicle reporting it
  from OEM1 gets it as a dealer-soon fault, and now shows a P0 card.

- **The iOS app hides any server line flagged `_synthetic_from_auto_book`,**
  including the confirmation shown after an automatic booking ("Done — you're
  booked …"). The fix is in the Meridian Motors Companion app (CMS repository).

- **`escalate_for_service` emits no escalation event.** It opens an Amazon
  Connect contact, but the app is never told to join it, so a human agent can
  answer a chat with nobody on the other side.

- **Tier 2 alarms have no notification target on stages other than staging.**
  The staging fix is proven; wiring the same customer-managed-key +
  `AlarmActions` pattern on any additional stage before relying on scheduled
  runs is left to the operator.

- **Tier 2 scan window is still fixed at 25 VINs per run** and does not
  advance, so it does not yet cover large fleets.

- **EVE (voice evaluation pipeline)** remains deferred; the voice agent is
  functional but not covered by automated evaluation.

## [0.2.3] — 2026-09-23

### Added

- **Session-memory persistence for text and bidi surfaces** (Groups 1–2) — AgentCore
  Memory API integration with fail-closed actor_id validation, server-side turn
  hydration on session init, and schema-versioned Config fields. Eliminates cold-start
  context loss, enabling multi-turn memory across text chats and voice sessions.
  
- **AVX Tier 2 platform-health reporting enhancements** (Groups 3–5) — new Finding and
  Action message schemas, DMS event consumer for findings publication, HTTP surface for
  read-only access, and 6-persona executor dispatch with demo Tier 2 agent provisioning.
  Findings now carry structured scope (owner / VIN / fleet / dealership / district),
  surfacing staleness via `computed_at` rather than hiding it.

- **Dealer-certified parts lookup** — `parts_intel` tool via Parts.com schema integration,
  seeded with 586 per-SKU parts documents and recall-narration anchors to OEM verbatim
  text. Mutation-guarded guarantee that narrations are pinned to seam constants.

- **DMS persona extension** — 5 dealer personas (service advisor, service manager, parts
  manager, fleet manager, dealership owner) with Cognito group gating, scope-based query
  enforcement, and certificate-tier guardrailling (PENDING/CERTIFIED/SUSPENDED flow).

- **Infrastructure cost attribution** — every AVX CloudFormation stack now tagged with
  cost-center and project metadata at app scope for cost allocation and billing analysis.

### Changed

- **Guidance renamed** — "Guidance for Connected Vehicle Experience on AWS" is now
  "Guidance for Agentic Vehicle Experience on AWS". The rename applies to the display
  name across README, deployment guide, Well-Architected doc, and evals guide.

- **Shortcode swapped `CVX` → `AVX`** — user-facing prose across README, WELL_ARCHITECTED,
  the docs/ folder, and this changelog now uses `AVX` as the shortcode, matching the
  new long-form name (`Agentic Vehicle Experience`). See
  `issues/2026-09-18-cvx-avx-shortcode-swap/` for the decision record.

- **Branding aligned to Meridian Motors** — all demo personas and tenant configurations
  now reference Meridian Motors instead of Ford/Michelin, and custom:tenantId is
  provisioned kebab-case for consistency with infrastructure naming.

- **Code identifiers unchanged** — `cvx-*` CloudFormation stack names, `vsa-*` AgentCore
  runtime names, the `AmazonBedrockAgentCoreSDKRuntime-{region}-vsa-{stage}` IAM role
  name (Bedrock allowlist-required), `CVX_*` env vars, and spec-slug directory names
  are preserved as legacy identifiers.

### Fixed

- **Session-memory API integration** — hydration path now grants ListEvents permission,
  fixing silent failure on session init when no prior turns existed. VSA_TOOL_MEMORY_READS
  gate now enforces read-only access on all 8 memory-reading tools.

- **Bidi voice fault handling** — exception messages are now sent to the client instead
  of being discarded; 55s idle timeout is documented; WebSocket upgrade verified
  end-to-end via live-verification gate.

- **Deploy safety** — ambient CMS_STAGE environment variable no longer silently overrides
  stage defaults in CDK synthesis; fail-closed when VSA_STAGE is unset; deploy footguns
  closed without Makefile changes.

- **Acquire lead flow** — leadId is now idempotent so retries cannot duplicate leads;
  Acquire block is no longer deleted by every deploy; seedling inject guard properly
  enforces injectivity via mutation verification.

- **Tier 2 agent provisioning** — Bedrock dependency unpinned range dropped at runtime;
  Tier 2 agent marked tokens_used=true correctly; marker-driven drill-down pre-seed now
  reads live marker file instead of using stale default.

- **Escalation robustness** — Connect resources use Meridian-branded names instead of
  PLACEHOLDER literals; tenant Connect config now wired correctly; both escalation and
  deployment defects from 2026-09-22 staging deploy have been resolved.

- **AVX findings/actions data plane** — read-surface now functional with proper GSI
  scoping; write-path integrated with DMS event consumer; earlier defects from Groups 3–4
  mutation-verified across all 40 hazard cases.

- **Publish safety** — account IDs in test fixtures replaced with placeholders; publish
  archive gate now fails at step 0 for any critical findings; three pre-publish
  vulnerabilities closed.

- **Seed stability** — persona seed no longer resurrects CMS rows; VEH-FORD-001/DRV-FORD-001
  retired; seeder preserves existing Cognito resources instead of clobbering them.

### Security

- **Credential exposure** — real account ID and Cognito pool ID leaked into pushed test
  data now replaced with placeholders; live-verify harness now fails honestly instead of
  misleadingly passing.

- **Authorization hardening** — dispatch gate now fail-closed so API stack can deploy at
  all; dealer scope correctly enforces Cognito group membership; SNS publish monopoly
  guard is mutation-verified 3 ways.

- **IAM policy tightening** — wildcard-pattern resources rejected via deterministic-seam
  test gate; cdk-nag enabled for static policy analysis; IAM Access Analyzer integration
  for access validation.

### Known Limitations

- **Tier 2 alarms have no notification target.** Both CloudWatch alarms ship with empty
  `AlarmActions`, so an unattended nightly failure produces a stale artifact that the
  conversational agent keeps narrating with a receding `computed_at`. Wire an operator
  SNS topic before relying on scheduled runs.

- **The Tier 2 scan window is fixed at 25 VINs per run** and does not advance, so it does
  not yet cover large fleets.

- **EVE (voice evaluation pipeline)** remains deferred; the voice agent is functional but
  not covered by automated evaluation.

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

- Numerous publish-safety fixes ahead of first publication.

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
