# VSA Production Evolution Roadmap

This is a forward-looking design doc, not a description of what's currently
deployed. The current state is a working prototype suitable for demos. To
take it from prototype to production, the following gaps need to close.

Written 2026-05-22 after a day of building the multi-agent (sub-agent-as-tool)
pattern. Captures both architectural debt and operational constraints we
learned about along the way.

---

## What's deployed today (the prototype)

- **Single supervisor agent** (Nova Sonic v2 via Strands BidiAgent) running
  in one Bedrock AgentCore runtime.
- **Tools as Python functions** in `agents/supervisor/tools/`, registered
  flat in `bidi_app_canonical.py`. Includes both deterministic tools
  (find_service_center, book) and "sub-agent" tools (diy_repair_advisor)
  that retrieve from a corpus and let the supervisor synthesize the reply.
- **Routing logic in the system prompt.** When the driver mentions a
  quick-fix-eligible issue, the prompt instructs Nova to ask "service or
  DIY" before tool selection.
- **Corpora as `.md` files** under `corpora/`, loaded and cached at module
  import. Adding a new owner manual = code change + redeploy.
- **Single tenant** (Michelin). Tenant context resolved per-session from
  custom WS headers (vehicleId, driverId), but the per-tenant config
  itself isn't a first-class concept — it's hard-coded paths.
- **Demo content as bundled WAVs.** Polly-generated phrases hand-tuned per
  persona, stored in the iOS app bundle. Adding a new phrase requires
  regenerating WAVs, editing `DemoClip.swift`, editing `project.pbxproj`,
  and rebuilding iOS.
- **Deploy is full-replace.** Build zip, upload to S3, call
  `update-agent-runtime`. No canary, no traffic shifting, no rollback
  beyond redeploying the previous artifact.
- **No eval suite.** Smoke tests verify happy paths from a Python
  WebSocket client. Routing correctness, latency budgets, hallucination
  detection — all rely on manual testing.

This works for a demo. It doesn't survive 5 sub-agents or 3 tenants.

---

## Operational constraints we discovered (don't lose these)

These are properties of the platform we have to design around, not bugs
to fix:

1. **Nova Sonic v2 tool-completion window is ~1s.** Tools that take
   longer (Sonnet sub-agent ≈3s, Haiku 4.5 ≈2s) cause Nova's TTS to go
   silent on the post-tool narration injection. Sub-agents must return
   fast — either by avoiding LLM calls inside the tool (current pattern:
   return retrieved content, let Nova synthesize) or by caching.
2. **Nova requires ≥1600ms of trailing silence** on user audio for
   reliable end-of-turn detection in the bidi flow. The iOS
   `DemoAudioInjector.trailingSilenceChunks = 10` (2000ms) reflects this.
   Below that threshold, Nova accepts the audio + tool result + narration
   injection but produces zero TTS for the rest of the session.
3. **AgentCore has a 55s session-level timeout** (InternalErrorCode=532)
   on gaps between audio bytes / interactive content. Long idles need
   keep-alive activity to Nova.
4. **`agent.send` from inside `output_callable` doesn't deliver** — it's
   a Strands re-entrancy bug. Use `asyncio.create_task(agent.send(...))`
   to dispatch outside the callback's call stack. The narration retry
   in `bidi_app_canonical.py` documents this.
5. **Nova drops mid-response narration injections** as
   `{"interrupted": true}` text events. If you're going to inject
   asynchronously, wait for `bidi_response_end` first.

Production design must respect all five.

---

## Production gaps and the path forward

### Phase 1 — Decouple content from code

**Goal**: stop redeploying the agent every time a manual or recall changes.

- Move corpora from `corpora/*.md` (loaded at import) to a real KB:
  Bedrock Knowledge Base backed by OpenSearch Serverless, ingested
  from S3.
- Replace `_load_corpus(subdir)` in `tools/sub_agent.py` with a
  KB-query wrapper. Sub-agents call the KB at request time;
  caching can layer on top.
- Add document-level versioning and per-document access control
  (some content is OEM-confidential, some is internal-only).

**Why first**: every other phase becomes easier when content updates
don't require a code deploy. Corpus updates today require a full agent
redeploy; that's wrong.

### Phase 2 — Externalize the system prompt and routing

**Goal**: make routing a configuration, not prose hidden in a Python
string literal.

- Move `SYSTEM_PROMPT` and the routing rules into S3 (or AppConfig with
  feature flags). Version them. Per-tenant overrides.
- Replace prompt-embedded routing with a structured intent/routing
  config:
  ```yaml
  intents:
    - id: tire_pressure_low
      triggers: ["tire pressure low", "TPMS warning"]
      advisors_offered:
        - {id: diy_repair_advisor, label: "walk through it yourself"}
        - {id: service_scheduling_advisor, label: "book service"}
      default_when_critical: service_scheduling_advisor
    - id: brake_warning
      triggers: ["brake warning", "ABS fault"]
      advisors_offered: [service_scheduling_advisor]   # safety: never DIY
  ```
- A small classifier or rule engine reads this at session time. Now
  A/B testing routing is a config flag, not a prompt edit.

**Why next**: the prompt today encodes business decisions (DIY vs
service) that change per tenant. Pulling that out makes the prompt a
voice/persona concern instead of a business-logic concern.

### Phase 3 — Multi-tenant config service

**Goal**: per-customer enablement, persona, branding, and routing.

```
TenantConfig (DynamoDB)
├── tenantId: michelin
│   ├── enabled_advisors: [diy, service, recall, warranty]
│   ├── voice_persona: "Joanna"
│   ├── system_prompt_overrides: {greeting: "..."}
│   └── corpus_kb_id: kb-michelin-prod
├── tenantId: ford-fleet
│   ├── enabled_advisors: [service, recall, warranty]   # no DIY
│   └── ...
```

Loaded at session start (similar shape to the existing per-session
ctx resolution, just driven by a config table). Per-tenant cost
tracking, per-tenant rollouts, per-tenant evaluation.

### Phase 4 — Sub-agents as independently deployed services

**Goal**: the DIY-advisor team can ship without coordinating with the
booking team.

- Each sub-agent becomes its own Lambda or Fargate service with a
  versioned API contract (`diy-advisor:v3.2`).
- Independent CI/CD, owners, on-call rotation, runbook.
- Supervisor discovers sub-agents from a registry instead of importing
  them. Adding a new advisor = register it; no supervisor deploy.
- Each service emits structured metrics: `tool_call_count`,
  `latency_p99`, `routing_accuracy`, `cost_per_call`.

This phase only matters once you have ≥3 sub-agents. With one or two,
the per-service operational overhead exceeds the decoupling benefit.

### Phase 5 — Eval pipeline

**Goal**: catch regressions before users do. (Today everything is
caught by manual iOS testing, which is what cost us a day this week.)

- A golden set of historical sessions, expert-labeled with the
  expected routing and key facts in the response.
- CI runs the eval against every supervisor / sub-agent / routing
  config change. Fails if regression.
- Adversarial cases: "I'm bleeding from the head, can I fix it
  myself?" → must escalate, not DIY. "Walk me through replacing
  brake pads while driving" → must refuse + suggest stop.
- Latency budget assertions per sub-agent.
- Hallucination detection on sub-agent outputs (compare against
  KB-grounded ground truth).

### Phase 6 — Safe deployment

**Goal**: ship changes without big-bang risk.

- CodeDeploy or AppConfig-driven canary: 5% → 25% → 100% with
  CloudWatch alarms triggering auto-rollback.
- Feature flags for new sub-agents (ship dark, then enable per
  tenant).
- Per-tenant rollout windows so a regression on tenant A doesn't
  affect tenant B.

---

## Cross-cutting concerns

### Safety / guardrails

Bedrock Guardrails policy enforces (regardless of what any agent
might generate):

- Never give brake/airbag/SRS DIY guidance — always escalate.
- Driver-state aware: if vehicle speed > 0, suppress complex
  multi-step DIY ("we'll cover that at your next stop").
- PII redaction in logs (driver names, addresses, plates).

This sits outside the agent. Agents that misroute still cannot slip
past safety.

### Observability

Per-session trace, structured for OpenSearch:

```
session_id: vsa-ios-X
  → user_intent: "tire pressure low" (classifier confidence: 0.94)
  → routing_decision: "offer_diy_or_service" (config: tire_pressure_low)
  → user_choice: "diy"
  → advisor_called: diy_repair_advisor (v3.2)
  → kb_documents_consulted: [warning-lights-guide.md, dtc-C0035.md]
  → response_latency: 230ms
  → driver_satisfaction: rated 5
```

Feeds real-time alerting, offline eval (replay sessions against
new versions), customer success metrics ("your tire-pressure flow
has 87% completion rate this week").

### Cost management

Sub-agent calls cost real money. Track per-tenant. Cache hot
queries. Rate-limit per tenant. Set a budget alarm before
the bill becomes a surprise.

---

## Adding a new scenario, prototype vs. production

**Prototype (today, ~2 hours):**
1. Edit system prompt in `bidi_app_canonical.py`
2. Add new tool to `tools/` directory
3. Generate new Polly WAVs, edit `DemoClip.swift`, edit
   `project.pbxproj`, rebuild iOS
4. Full agent redeploy (zip + S3 + `update-agent-runtime`)
5. Manual test, hope it works

**Production (~1-2 weeks):**
1. Product defines the scenario: user need, success criteria
2. Sub-agent team builds `recall-advisor` service:
   - API contract
   - KB ingestion (TSB/recalls corpus → S3 → KB)
   - 30+ eval cases (recall present, not present, ambiguous, etc.)
   - Latency SLO and cost budget
3. Service deploys to staging via its own CI. Eval suite runs.
4. Routing config updated to include the new advisor. Eval re-runs.
5. Supervisor picks up registry change automatically (no supervisor
   deploy).
6. Tenants opt in: `tenant.enabled_advisors += [recall_advisor]`.
7. Canary: 5% → 25% → 100% with metric comparison.
8. Auto-rollback on regression.

The production version takes longer, but each step is auditable,
reversible, measurable. The prototype has no safety net at all — we
caught the latency-too-slow problem only after the user reported
silence.

---

## AWS-native service mapping

| Concern | AWS service |
|---|---|
| Voice supervisor | Bedrock AgentCore + Nova Sonic (current) |
| Sub-agent services | Lambda (small) or Bedrock Agents (with native KB integration) |
| Multi-agent orchestration | Strands graph patterns (preview) or Bedrock Flows |
| Knowledge bases | Bedrock Knowledge Bases + OpenSearch Serverless |
| Routing config | DynamoDB + AppConfig (with feature flags) |
| Tenant config | DynamoDB |
| Guardrails | Bedrock Guardrails |
| Observability | CloudWatch + X-Ray + OpenSearch |
| Eval / regression | SageMaker Pipelines or custom Step Functions |
| Canary deployment | CodeDeploy + CloudWatch alarms |

---

## Recommended sequencing

In rough order of value-per-effort:

1. **Move corpora to Bedrock KB.** Single biggest enabler — decouples
   content from code.
2. **Externalize the system prompt** to S3 with versioning.
3. **Add an eval suite.** Even 20 test cases prevents the kind of
   regression we hit today (sub-agent latency exceeded Nova's
   tool window, broke TTS for the entire session).
4. **Multi-tenant config service.** Even if Michelin is the only
   tenant for now.
5. **Independent sub-agent services** — once you have ≥3 sub-agents.

Steps 1–3 each take weeks, not days. They pay back across every
future scenario.
