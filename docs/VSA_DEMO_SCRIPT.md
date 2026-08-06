# VSA Demo Script — Exec/Customer Briefing

A ~7-minute live briefing-room demo against the Cascadia tenant. Three back-to-back scenarios on the same VIN (VEH-0025) escalating from P3 → P1 → P0. Voice on the primary, companion app projected on the screen.

This script is for the presenter. It includes: pre-flight checklist, exact presenter dialog, exact fault-injection commands against the CMS simulator, timing cues, and what the audience should see on the companion app during each scene.

---

## 1. Pre-flight (do before walking into the room)

**30 minutes before.**

1. Confirm CMS simulator is running and VEH-0025 is on the Dallas → Memphis route.
   - Check: CMS UI shows VEH-0025 with a green status on the map, moving.
   - If stopped: `./services/simulation/manage_simulation.sh start --vehicle VEH-0025 --route dallas-memphis`
2. Confirm Connect is reachable.
   - Dial the Cascadia DID (`+1-800-555-1200`) from a mobile phone; hear the tenant greeting; hang up.
3. Confirm companion app loads with the Cascadia theme.
   - Open `https://michelin.vsa.example.com/?debug=1` in the presentation browser tab.
   - Verify: Cascadia logo, primary blue, yellow accent, VEH-0025 visible in the left pane, reasoning drawer collapsed at the bottom.
4. Confirm tenant config version.
   - Hit `GET /vsa/tenant/michelin/config` — should return `version: "1.0.0"`, `status: "active"`.

**5 minutes before.**

1. Open three browser windows side by side:
   - Window 1: Companion app (Cascadia theme) — **this is the main projection**.
   - Window 2: Presenter control panel (`?debug=1` reveals the fault-injection buttons).
   - Window 3 (optional, for Q&A only): CMS fleet UI showing VEH-0025 telemetry.
2. Silence notifications, set audio output to conference system, test mic.
3. Have the mobile phone you will call from in hand, audio routed to the room speakers.
4. Open `VSA_DEMO_SCRIPT.md` (this doc) on a second monitor as your teleprompter.

---

## 2. Setup (30 seconds)

**Projection:** Companion app, Cascadia theme. Left pane shows VEH-0025 on a moving map, all-green telemetry. Right pane shows the chat interface, empty. Reasoning drawer collapsed.

**Presenter says:**

> "Cascadia manages a 200-truck fleet across North America. Today we're with VEH-0025 — a Freightliner Cascadia, headed from Dallas to Memphis. Driver is T. Hassan. Everything looks normal. Let's see what happens when the vehicle tells the driver something's off."

**Audience cue:** point at the green telemetry in the left pane, move the cursor along the route to draw their eye to the moving vehicle.

---

## 3. Scene 1 — P3 Monitor: Tire pressure dip (90 seconds)

### Trigger

Click the presenter control panel button **"P3: Tire pressure dip FL → 32 PSI"**.

Equivalent call (for reference; the button does this):

```
POST /simulator/inject
{ "vin": "1FMCU0F70MUA12345", "signal": "tire_pressure_fl", "value": 32, "duration_s": 60 }
```

### Expected behavior

Within 3–5 seconds the companion app shows:
- Left pane tire pressure FL goes from 34 → 32 (amber flash, no red).
- A push notification appears on the companion app: "Tire pressure alert — front-left 32 PSI."

### Presenter action

Click the notification (or in a real scenario, the driver would tap it). This opens the chat pane in the companion app. Use chat, not voice, for this scene — keeps it fast.

### Dialog

Type into the chat pane:

> **Driver (you):** "Got a tire pressure alert — am I good?"

**VSA response (streams in, ~1.5s latency):**

> "Both front tires are at 32 PSI, spec is 34. You're 2 PSI below target — well within safe range. No action needed right now. I'll keep an eye on it and check back in 30 minutes. Safe travels."

### Reveal the reasoning drawer

**Click to expand the reasoning drawer at the bottom of the app.**

**Audience sees:**

```
Triage: P3 Monitor
Classifier version: 1.0.0
Inputs checked (87ms):
  ✓ Active DTCs: none
  ✓ Telemetry:
     tire_pressure_fl = 32, threshold P2 at 30 (spec 34 − 4) → no contribution
  ✓ Open recalls: none
  ✓ Predictive signals: none
Decision: P3 (baseline)
Tool calls: 0 external
```

### Presenter says

> "P3 — monitor. The classifier is deterministic — not the LLM. It checked the threshold, saw 32 PSI is within tolerance, and returned P3. The LLM's only job was to turn that into a sentence in Cascadia's voice. Notice the audit trail — everything it checked, the exact thresholds, in 87 milliseconds."

**Timing check:** total scene 90s. If running long, skip the drawer narration and move on.

---

## 4. Scene 2 — P1 Service Now: Coolant + DTC + Recall (2 minutes)

### Trigger

Click presenter button **"P1: Coolant overtemp + DTC P0217"**.

Equivalent calls:

```
POST /simulator/inject
{ "vin": "1FMCU0F70MUA12345", "signal": "coolant_temp", "value": 118 }

POST /simulator/dtc
{ "vin": "1FMCU0F70MUA12345", "code": "P0217", "severity": "critical" }
```

### Expected behavior

Within 3–5 seconds:
- Left pane: coolant temperature gauge goes red, reads 118°C.
- DTC P0217 appears in the active DTC list with a red dot.
- A higher-urgency notification appears: "Engine warning — temperature critical."

### Presenter action

This scene is voice — pick up your mobile phone and call the Cascadia DID on speakerphone so the room hears both sides. The companion app shows the live transcript streaming in the chat pane as the conversation happens.

### Dialog (spoken)

**Prompt (Connect plays):**

> "Cascadia Fleet Support. I can see your vehicle in real time. How can I help?"

**Driver (you, on the phone):**

> "Temperature gauge just spiked."

**VSA (Nova 2 Sonic, sub-700ms latency):**

> "I see it. Your coolant is at 118°C and I'm also seeing a fault code that just came in — P0217, engine over-temperature. Here's what matters: there's an open recall on your coolant temperature sensor that likely explains this. This is a service-now situation — let's get you to a shop within the next 48 hours.
>
> Good news: Rush Truck Center in Dallas North is 18 miles ahead, and it's in your preferred network. I've drafted a service ticket — ticket number VSA-2026-04-27-4821.
>
> Because you're on a Tier-1 fleet SLA, I can also connect you right now to a fleet ops specialist who can make sure the shop is ready when you arrive. Want me to connect you?"

**Driver:**

> "Yes, please."

**VSA:**

> "Connecting you now. The specialist will have all the context — your location, the fault, the recall, and the draft ticket. One moment."

### On the projection

As the voice dialog is happening, the companion app:

1. Streams the live transcript in the chat pane.
2. Reasoning drawer auto-expands.
3. A side panel slides in showing the STAR-JSON ticket payload being built in real time (fields populate as they're resolved — VIN, customer, service center, symptoms, warranty check).
4. When warm transfer is accepted, a "Transferring to tier-2 fleet ops" banner appears, then transitions to a mini Connect agent view showing the tier-2 agent's screen with all the context pre-populated.

### Reveal the reasoning drawer (audience sees)

```
Triage: P1 Service Now
Classifier version: 1.0.0
Inputs checked (243ms):
  ✓ Active DTCs:
     P0217 — engine over-temperature (critical, powertrain, limited-safe) → P1
  ✓ Telemetry:
     coolant_temp_c = 118, threshold P1 at 115 → P1 (primary)
  ✓ Open recalls:
     26V123456 — coolant sensor (powertrain) → escalator, system overlap
  ✓ Predictive signals: none
Tenant rules evaluated:
  ✓ michelin-tier1-warm: fleetSla == 'tier-1' && classification == 'P1'
     → forced action: force-warm-transfer
Decision: P1
Forced actions: [force-warm-transfer]
Tool calls (6):
  • adp.c360.lookup_by_phone → cust-1234 (Cascadia, Tier-1)
  • cms.redis.get_vehicle_state(VIN) → coolant 118, speed 58
  • cms.recall.get_open(VIN) → 26V123456
  • adp.warranty.check(VIN, coolant_sensor) → covered, no deductible
  • adp.parts.lookup_by_component(freightliner, cascadia, 2023, coolant_sensor)
  • vsa.booking.draft_ticket → VSA-2026-04-27-4821
```

### Presenter says (after transfer is audibly completed)

> "That's the full loop. Classification deterministic. Warranty pre-checked. Ticket drafted as STAR-JSON. Warm transfer with context pre-populated to the tier-2 fleet ops queue. The tier-2 agent opened this call with everything they needed. No cold handoff, no 'can you repeat your VIN,' no lost minutes."

**Timing check:** total scene 2m. The voice call should run ~45 seconds; the drawer explanation ~30 seconds; the recap ~20 seconds.

---

## 5. Scene 3 — P0 Stop Driving: Brake fault (90 seconds)

### Trigger

Click presenter button **"P0: Brake fault + ABS disabled"**.

Equivalent calls:

```
POST /simulator/dtc
{ "vin": "1FMCU0F70MUA12345", "code": "C1234", "severity": "critical" }

POST /simulator/inject
{ "vin": "1FMCU0F70MUA12345", "signal": "abs_status", "value": 0 }
```

### Expected behavior

Within 3–5 seconds:
- Left pane: the entire left pane border flashes red briefly.
- DTC C1234 appears; ABS status indicator goes red.
- A critical-priority notification fires; if the driver were in chat, the companion app would auto-open a voice session. For the demo, the driver calls.

### Presenter action

Call the Cascadia DID again. Keep it short — the point is the P0 is fast, firm, and leaves no room for LLM softening.

### Dialog (spoken)

**VSA (Nova 2 Sonic, picks up immediately because the session is already flagged P0 by the telemetry event):**

> "T, this is Cascadia Fleet Support. Your brake system is reporting a critical fault and ABS is disabled. I need you to pull over safely right now. I'm dispatching roadside assistance to your GPS location, and I'm connecting you to a human specialist. Stay on the line."

**Driver:**

> (no need to say anything — that's the point)

### On the projection

Reasoning drawer auto-expands:

```
Triage: P0 Stop Driving
Classifier version: 1.0.0
Inputs checked (198ms):
  ✓ Active DTCs:
     C1234 — brake system fault (critical, brakes, NOT safe to drive) → P0 (primary)
  ✓ Telemetry:
     abs_status = 0 → P0 (secondary)
     speed_mph = 58 → contextual, no contribution
  ✓ Open recalls: none
Decision: P0
Forced actions from classifier: [auto-escalate-tier2, dispatch-roadside]
Tenant rules evaluated:
  (none triggered — P0 classifier forces escalation regardless)
Tool calls (3):
  • vsa.roadside.dispatch(VIN, location) → dispatch-87c21
  • connect.escalate_to_queue(tier2-ops, context-bundle)
  • vsa.audit.log_p0_event
Guardrail audit:
  ✓ LLM utterance passed P0 validator (no banned phrases)
  ✓ No downgrade attempted
```

### Presenter says

> "P0 is a different animal. The classifier saw a critical brake fault, marked it P0, and the system forced immediate action — roadside dispatched, tier-2 specialist paged, session locked. The LLM's output was validated for banned phrases before it left the mouth — 'don't worry,' 'should be fine,' anything that could soften a P0 gets caught. This is the auditability story. Regulators, safety reviewers, warranty counsel — all of them can replay any decision, byte for byte."

**Timing check:** total scene 90s.

---

## 6. Architecture Recap (60 seconds)

**Switch projection to:** a single static slide with the architecture diagram (from `ARCHITECTURE.md`) showing VSA sitting above VFO + CMS + ADP + Connect.

**Presenter says:**

> "What you just saw is a single tenant — Cascadia — on the Virtual Service Agent layer. It reads live telemetry from the Connected Mobility platform, warranty and service history from the Automotive Data Platform, and it talks through Amazon Connect and a branded web app. Every tenant has its own config: branding, voice, policies, escalation rules, tier-2 queues. Ford and Avis are next — same stack, different skins, different rules.
>
> Three things to take away: it's deterministic where it matters, it's auditable end-to-end, and it's tenant-neutral by design. One build, many customers."

---

## 7. Q&A-ready cheat sheet

Quick answers the presenter should have memorized:

| Likely question | Short answer |
|---|---|
| "How does the AI not hallucinate a safety call?" | Classification is deterministic code. LLM narrates only. Output validator catches banned phrases. See `VSA_TRIAGE_CLASSIFIER.md`. |
| "How is this different from a standard IVR or Lex bot?" | IVR has no context. Lex has intent but no live data. VSA fuses live telemetry, DTC history, recalls, warranty, service history, and closes the loop with a STAR-JSON ticket and warm transfer. |
| "How long to onboard a new tenant?" | Config change — no code. New branding, greetings, queues, policies in a JSON file. Specialist agents specific to a tenant are optional Lambdas. See `VSA_TENANT_CONFIG.md`. |
| "What if CMS or ADP is down?" | Graceful degradation. Each input has a fallback. A session never proceeds with low-confidence; it escalates to tier-2. See `VSA_INTEGRATION.md §7`. |
| "What's the STAR connection?" | All bookings emit as STAR-shaped JSON (maps 1:1 to STAR XML). Tenants already on STAR plug in directly. See `VSA_STAR_JSON_SCHEMA.md`. |
| "What's the voice engine?" | Amazon Nova Sonic — speech-to-speech, streaming, low-latency in the cab. Polly neural as fallback. |
| "Can we demo Ford or Avis instead?" | Yes with a subdomain switch. Currently only Cascadia is fully configured in v1. |

---

## 8. Recovery scripts (if something breaks live)

| Symptom | What to do |
|---|---|
| Simulator not running | "Let me show you the pre-recorded version" — play the backup video (`demos/vsa-exec-briefing-v1.mp4`). Continue narrating live. |
| Connect call drops mid-scene | Switch to chat channel in the companion app. Every scenario works in chat; narrate the voice-specific differences. |
| Classifier returns unexpected level | Do NOT argue with the drawer. Say: "That's a live system — today's classifier saw this particular combination of inputs and landed there. The trace shows why. This is exactly the auditability the system is built to give us." |
| Companion app won't load the theme | Reload with `?tenant=michelin&refresh=1`. If still broken, fall back to CMS fleet UI and a slide showing what the app looks like. |
| P0 validator refuses to let the LLM speak | This is the system working. Say: "Good — the guardrail just fired. The LLM tried to phrase this too softly and the validator rejected it. You're watching a safety mechanism in action." |

---

## 9. After the demo

1. Share the doc bundle: `VIRTUAL_SERVICE_AGENT.md`, `VSA_INTEGRATION.md`, `VSA_TENANT_CONFIG.md`, `VSA_TRIAGE_CLASSIFIER.md`, `VSA_STAR_JSON_SCHEMA.md`, and this script.
2. If the audience asked about a specific tenant (Ford, Avis), send them the corresponding stub config from `VSA_TENANT_CONFIG.md` §5–§6 as a preview.
3. Capture the Q&A, update §7 above with new question patterns.
4. If a live issue broke, file an issue against the companion app or simulator before the next briefing.

---

## 10. Open Items for Future Demo Variants

1. **Ford variant** — same scenarios, OEM warranty coverage deeper, account-number auth instead of phone lookup.
2. **Avis variant** — mid-rental scenario (tire blowout on highway), force-tier2 on any P1, swap-vehicle offer via rental-policy specialist agent.
3. **Fleet-manager persona** — B2B variant where a fleet manager asks about multiple vehicles, reuses VFO supervisor directly (v2+).
4. **Image-input variant** — dashboard photo upload via companion app, Nova Pro vision extracts warning lights, feeds triage (v3).
5. **Offline/replay demo** — pre-canned scenarios for venues where live Connect or CMS access is unreliable.
