# CVX Demo Runbook

Reusable runbook for demoing the Connected Vehicle Experience (CVX) agent platform
to internal (or customer) audiences. Pairs with `docs/DEPLOYMENT.md` (how to deploy)
and `docs/ARCHITECTURE.md` (how it's built). This doc is about **how to show it**.

> **Owner:** Product. **Last validated:** update this line each time you run the demo
> against staging (date + which surfaces/personas you confirmed green).

---

## 1. The pitch (your framing)

CVX is a **multi-persona connected-vehicle agent platform**: drivers, owners, and
service staff get real-time, **grounded**, **safety-gated** assistance over voice or
text — backed by live vehicle data (ADP) and a deterministic triage layer the LLM
cannot override.

Four things make it demo-worthy. Every demo should land at least three:

1. **Safety-first deterministic triage** — the agent *must* call the triage classifier
   before narrating any severity (P0–P3); it cannot hallucinate or downgrade. P0
   auto-escalates roadside.
2. **Grounded answers** — recalls, warranty, part numbers, and policy come from the
   ADP knowledge base (live KB `YULHRAUMNU`), never invented. Ask something not in the
   KB and it says "I don't have that" instead of guessing.
3. **One platform, many personas/tenants** — the same supervisor binds different tools
   + data products per persona, and tenant segment changes behavior (e.g., OEM routes
   only to authorized dealers).
4. **Voice** — Nova Sonic real-time bidi on the iOS VSACompanion app.

---

## 2. Where you demo — surfaces

| Surface | Entry point | Runtime / region | Reliability | Use for |
|---|---|---|---|---|
| **Text CLI** | `python -m agents.supervisor.cli` | `vsa_supervisor` (text) | ★★★ rock-solid | The substance: triage, KB grounding, personas |
| **iOS VSACompanion** | iOS app (CMS repo `clients/ios/VSACompanion`) | `vsa_supervisor_bidi_staging`, us-west-2 (Nova Sonic cross-region → us-east-1) | ★★ only if `Staging.xcconfig` fully back-filled | The "wow": live voice P0 flow |
| **REST API** | `curl` against `$VSA_API_BASE` (`api-assistant-chat` / endpoints) | `vsa-staging-api` | ★★★ | Backup text path; showing the API |
| **CVX web UI** | browser | in flight (`2026-06-19-cvx-web-ui-surfacing`) | ✗ not demo-ready | Skip until shipped |

**iOS caveat (read before relying on voice):** `make deploy-staging` auto-fills only
`VSA_AGENTCORE_RUNTIME_ARN` in `clients/ios/VSACompanion/Config/Staging.xcconfig`. The
other six placeholders (REST URL, WS URL, telemetry WS, three Cognito IDs) require
manual back-fill from CFN outputs — see CMS issue
`2026-05-28-backfill-staging-xcconfig`. If those are blank, the iOS app will not
connect to staging. Confirm before promising voice, or fall back to the text CLI.

---

## 3. What clients — tenants & personas

| Tenant | Segment | Named persona | What's distinct to show |
|---|---|---|---|
| **Cascadia** (`michelin`) | fleet | Samantha Carter | Broadest service-center mix; primary seeded tenant — safest demo |
| **Halcyon** (OEM) | oem | Marcus Reyes | Service routing restricts to **authorized Halcyon dealers only** |
| **Enterprise** | fleet | Priya Shah | Enterprise fleet framing |

Expansion personas (seeded via `scripts/seed-personas.py`): `vehicle_owner`,
`service_advisor`, `technician`. Service advisor / technician get KB-grounded TSB, DTC,
and parts answers (`knowledge` + `parts_lookup` tools).

**Tool surface by persona** (from `agents/supervisor/tools/__init__.py`):
all personas have `triage`, `retrieve`, `book`, `escalate`, `vehicle_identity`,
`service_network`. Driver/owner add service/charging/OTA history; service_advisor /
technician add `knowledge`, `parts_lookup`, `diy_repair_advisor`.

**Demo seed credentials** (dev only): `demo@michelin.example`, password from
`$DEMO_PASSWORD`. If that variable is unset, `scripts/seed-persona-users.py` generates a
random 20-character password per run and prints it — it no longer falls back to a shared
literal. The previously-documented fixed password has been removed from this runbook: it
was force-set on every persona by any run that omitted `$DEMO_PASSWORD`, and it is a
`forbidden_strings` canary in `.publish-secrets-scan.yml`, so naming it here published it.
Default VIN `1HGBH41JXMN000024` → `VEH-0025` / `DRV-0055`.

---

## 4. Pre-flight checklist (do the morning of, not live)

Run from the CVX repo root with `.venv` active and staging credentials.

**One-command path (preferred):**

```bash
make demo-preflight
```

This runs every check below in sequence, prints a GREEN/RED/WARN readout per
check, exits non-zero if any hard check fails, and auto-resolves the bidi
runtime id from `.bedrock_agentcore.yaml`. It is read-only and
non-interactive — safe to chain in CI/cron. Required: AWS credentials with
read access in us-west-2 + us-east-1, and a Cognito JWT (`VSA_USER_TOKEN`
env, or a cached `~/.vsa/tokens.json` from prior login). `VSA_ADP_KB_ID`
defaults to `YULHRAUMNU`.

The KB-retrieve check is a soft-warn on a missing `s3://` citation but
hard-fails on a non-zero script exit. Voice/iOS Staging.xcconfig is a NOTE
only (out of scope for this repo) — see the iOS caveat in § 2 and CMS issue
`2026-05-28-backfill-staging-xcconfig` before relying on voice.

**Manual checks (what `make demo-preflight` runs, in order):**

```bash
# 1. Confirm staging stacks are healthy
aws cloudformation describe-stacks --region us-west-2 \
  --query 'Stacks[?starts_with(StackName,`vsa-staging-`)].[StackName,StackStatus]' --output table

# 2. Confirm the bidi runtime is READY
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <vsa_supervisor_bidi_staging id> --region us-west-2 \
  --query 'status' --output text   # expect READY

# 3. Text agent smoke (substance path)
python3 scripts/smoke-test-agentcore.py

# 4. Voice smoke (only if demoing iOS voice)
python3 scripts/smoke-test-voice.py

# 5. KB grounding smoke (proves recall/warranty answers are live, not local fallback)
python3 scripts/smoke-test-kb-retrieve.py --stage staging

# 6. Per-persona smoke — audit shows 6/6 green; re-confirm
export AWS_REGION=us-west-2
export RUNTIME_ID=<vsa_supervisor_bidi_staging runtime id>
export VSA_TENANT_ID=michelin
pytest evals/runner/tier3_e2e.py -k persona_smoke --staging -v
```

**Pass criteria:** all stacks `*_COMPLETE`, runtime `READY`, smokes exit zero, KB smoke
cites at least one `s3://` source. If the KB smoke falls back to local corpora, set/verify
`VSA_ADP_KB_ID=YULHRAUMNU` in the runtime env (DEPLOYMENT.md § "KB tools silently fall
back to local corpora").

**If iOS voice:** open the app, confirm it connects to staging (not a blank/again screen),
and run one throwaway P0 turn so you're not debugging live.

### 4a. Cold-start warm-up (avoid the demo-day 504)

**Symptom:** the first request after ~15 min of idle 504s at the API Gateway
29s hard timeout. Subsequent requests within the warm window return in 3–8s.

**Cause (breakdown):** Lambda cold start ~0.5–1s + AgentCore worker
container cold start 5–15s + Strands `Agent` + `BedrockModel` init 1–2s +
`persona_loader.load_persona` 6–7s (P3 `Persona-loader latency` on backlog,
unfixed at v1) + first LLM call 3–5s + retrieve + narration 1–3s = 16–33s
worst case. API-GW REST hard limit is 29s and is **not configurable**.

**Surface it applies to:** the web-UI text path
(`vsa-staging-api` → `api-assistant-chat` Lambda → text runtime
`vsa_supervisor_text_staging`). The iOS voice bidi path uses a
long-lived WebSocket + AgentCore-Runtime bidi and does not trip the same
29s cap, but its own cold-start budget is similar — apply the same
warm-up discipline.

**v1 mitigation — run before the demo, always:**

```bash
# From the CVX repo root with staging AWS creds + a valid IdToken.
# Fires a real request through /assistant/chat to warm the Lambda,
# the AgentCore container, and the persona-loader LTM cache.
python3 scripts/smoke-test-assistant-chat.py \
  --api-url "$VSA_API_URL" \
  --prompt "Warm-up check — please respond briefly to confirm you're online."
```

Confirm HTTP 200 in the terminal. If it 504s, run it again — the second
run always succeeds. Do this within the last 3–5 minutes before the
demo. If more than ~15 minutes elapse between the warm-up and the first
live prompt, run the warm-up again.

**Do NOT skip this step.** The `make demo-preflight` block above already
runs `smoke-test-agentcore.py` and `smoke-test-kb-retrieve.py`, which
warm the same runtime; if you run `make demo-preflight` <15 min before
the demo, the warm-up is implicit and you don't need a separate call.
If preflight ran earlier in the day, fire the one-liner above right
before you present.

**v1.1 fix (deferred, tracked on backlog):** provisioned concurrency on
`api-assistant-chat` + shrink the persona-loader budget (P3 backlog
`Persona-loader latency`) + evaluate whether the API-GW 29s cap needs a
WebSocket alternative for genuinely-cold prod traffic. Do not force
provisioned concurrency in v1 unless the warm-up step above proves
insufficient in practice.

---

## 5. The 15-minute script

Budgeted for internal users. Do the substance on **Cascadia via text CLI**; add **one
iOS voice flow** only if pre-flight confirmed it; close with a **Halcyon-vs-Cascadia**
multi-tenant contrast.

| Time | Segment | Surface · Client | What you do |
|---|---|---|---|
| 0:00–1:30 | **Framing** | slide | One slide: persona matrix + "deterministic safety + grounded data" |
| 1:30–5:00 | **P0 safety triage** (showstopper) | CLI · Cascadia | *"My brake warning light just came on and the pedal feels soft."* → agent leads with pull-over, auto-escalates roadside (Amazon Connect), books ticket without waiting |
| 5:00–8:00 | **Grounded recall/warranty** | CLI · Cascadia | *"Is there an open recall on my vehicle?"* then *"Is my battery still under warranty?"* → cites real KB data; then ask something off-corpus to show it declines instead of inventing |
| 8:00–11:00 | **Persona routing** | CLI · service_advisor | *"What's the service procedure for DTC P0301?"* and *"What part number do I need?"* → different toolset (knowledge, parts_lookup) on the same platform |
| 11:00–13:30 | **Voice + graceful severity** | iOS · Cascadia | Voice: *"My coolant temp is reading high"* (P1 → guidance + offer ticket), then *"my tire pressure light is on"* (P3 → monitor, no ticket) |
| 13:30–15:00 | **Multi-tenant + wrap** | CLI · Halcyon vs Cascadia | *"Where's the nearest service center?"* on Halcyon (dealer-only) vs Cascadia (broad mix); recap 3 pillars; tease roadmap (web UI, telemetry/energy products via PySpark-Glue) |

If iOS voice is not confirmed in pre-flight, replace 11:00–13:30 with the P1/P3 contrast
in the **text CLI** and spend the freed time on the multi-tenant contrast.

---

## 5a. Tenant scenario spine — severity contrast

The spine is two tenants at opposite ends of the triage scale, proving the classifier
**matches** severity rather than over- or under-reacting (same deterministic engine, both ends).

**Halcyon (OEM) — P0 critical DTC** (text CLI; reliable drama)
- Prompt: "DTC P0301 is stored and the engine is misfiring badly — what should I do?"
  (or the brake-P0 line). Lands **P0** → pull-over-first → auto roadside → book without waiting.
- OEM service routing = **authorized Halcyon dealers only** (segment contrast with Cascadia).

**Cascadia (fleet) — low tire pressure** (on-brand; calm end)
- Lands **P3 (monitor)** — tested path (`p3_tire_pressure.json`).
- Text CLI prompt: "The tire pressure warning just came on for vehicle <VIN> — what should I do?"
  → P3, calm guidance, nearest **tire specialist** (fleet routing returns a broad mix), confirmation-gated book.
- Voice/iOS brand moment (only if iOS is verified): follow with "Can I just top it off myself?"
  → `diy_repair_advisor` surfaces the **recommended PSI from the owner-manual KB** on-screen, then offers
  to book. NOTE: `diy_repair_advisor` is on the voice/bidi path, **not** the `fleet_driver` text persona —
  so the PSI/DIY beat is voice-only; the text CLI gives triage + tire-shop routing + book.

**Seeding**: none required. Low tire pressure is **not a stored DTC** (it's a TPMS warning); both
scenarios are *reported symptoms* the driver types/speaks and triage rates live. Just use a **seeded
Cascadia vehicle** (so `book()` can write a service-history row) — e.g. the demo VIN/VEH from
`scripts/seed-michelin.py`. Do **not** rely on live per-tire PSI (telemetry_summary is PySpark-gated).

## 6. Question bank — known-live tools

**Driver / consumer (Cascadia / Enterprise):**
- "My brake light is on and the pedal feels soft" → **P0** (pull over + roadside)
- "My coolant temperature is high, what should I do?" → **P1** (guidance + offer ticket)
- "My tire pressure light is on" → **P3** (monitor, no ticket unless you insist)
- "Is there a recall on my vehicle?" → KB-grounded recall
- "Is my battery still under warranty?" → KB-grounded warranty
- "When was my last service?" / "What's my charging history?" / "Any pending software updates?"
  → service / charging / OTA history
- "Where's the nearest service center?" → `service_network` (broad mix on fleet)

**Halcyon (OEM) — show the contrast:**
- "Where's the nearest service center for my Halcyon?" → authorized **Halcyon dealers only**

**Service advisor / technician:**
- "What's the service procedure for DTC P0301?" → KB (`knowledge`)
- "What part number do I need for [component]?" → `parts_lookup`
- "Show me this customer's interaction history" → `customer_profile` / `interaction_history`

---

## 7. DO NOT ASK — gated tools (will return "not available")

`telemetry_summary` and `energy_summary` are **PySpark-gated**: their source Glue tables
ship with the `PySpark via Glue` initiative (not yet deployed), so the tools return a
"not yet available" message by design. Asking these live makes the platform look broken.

- ❌ "Show me my telemetry trends over the last week"
- ❌ "What's my energy usage / efficiency?"
- ❌ Anything depending on `vehicle_telemetry_aggregated` or `energy_usage`

---

## 8. Voice-specific guidance

- Nova Sonic doesn't reliably trigger multi-tool chains (it uses direct-call bypass paths
  for triage/book/escalate — that's why P0 voice works). Keep voice to **single-intent**
  P0/P1/P3 flows. Do multi-tool questions in **text**.
- Voice replies are intentionally short (1–2 sentences) and point at on-screen cards.
  Don't expect it to read full tables aloud — that's a feature, not a gap.
- If voice disconnects mid-turn, the cause is usually an upstream KB/tool misconfig — see
  DEPLOYMENT.md § "Voice agent disconnects mid-turn". Fall back to text and move on.

---

## 9. Backup plan (if something breaks live)

1. **First-prompt 504 (cold-start)** → run the § 4a warm-up one-liner and
   re-ask. If you skipped § 4a, this is why. Repeat once — the second
   attempt always succeeds within the warm window.
2. **iOS won't connect** → run the same questions in the **text CLI** (same backend).
3. **KB answers look generic / cite no source** → `VSA_ADP_KB_ID` likely unset in the
   runtime; pivot to triage + persona-routing demos (no KB dependency) and note KB is a
   config toggle.
4. **A persona errors** → drop it from the script; Cascadia fleet_driver is the most
   reliable path.
5. **Total backend outage** → fall back to a pre-recorded screen capture of the P0 voice
   flow. Keep one in your slides as insurance.

---

## 10. Quick troubleshooting (live)

| Symptom | Likely cause | Fast action |
|---|---|---|
| First prompt 504 | Cold-start >29s API-GW cap (§ 4a) | Re-ask (second attempt is warm); repeat § 4a warm-up |
| KB answers generic | `VSA_ADP_KB_ID` unset | Skip KB demo; use triage/personas |
| Tool returns "not available" | PySpark-gated product | You asked a gated question — rephrase |
| iOS blank/won't connect | `Staging.xcconfig` not back-filled | Switch to text CLI |
| Voice silent after tool call | empty tool result upstream | Fall back to text |
| 401 on smoke | stale cached token | Clear `~/.vsa/tokens.json` or set `VSA_USER_TOKEN` |

---

## Appendix — grounding sources

- Tool surface & persona matrix: `agents/supervisor/tools/__init__.py`,
  `agents/supervisor/persona_definitions.py`
- Triage hard rules: `agents/supervisor/supervisor.py` (`_HARD_RULES`)
- Voice prompt & routing: `agents/supervisor/bidi_app_canonical.py`
- Deploy / endpoints / smokes: `docs/DEPLOYMENT.md`
- Gated products & KB fallback: `docs/DEPLOYMENT.md` § CVX-on-foundation v1 troubleshooting
