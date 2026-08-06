# CVX Request Flow Steps

Numbered, sentence-level flow steps describing how a single request traverses the CVX architecture, extracted and formalized from [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md), [`docs/DEPLOYMENT.md`](./DEPLOYMENT.md), and [`docs/persona-mapping.md`](./persona-mapping.md). Style follows the numbered, one-sentence-per-step convention used by CMS's README "Architecture (FWE mode)" section — each step ends at the next data store, service, or tier boundary.

Two request paths are documented separately because they diverge at the AgentCore Runtime tier: the **web-chat path** (text; CMS UI → REST) and the **voice/bidi path** (audio; iOS companion app → bidirectional stream). Steps 1–3 diverge (client + transport + auth); steps 4 onward converge on the same session-init, tool dispatch, and grounding surfaces, and only re-diverge at the response tier.

---

## Web-chat path (CMS UI → `/assistant/chat`)

Applies to the CMS fleet manager web UI's ChatAgent surface. Authoritative for the `vsa_supervisor_text_staging` AgentCore text runtime.

1. Browser sends the operator's prompt over HTTPS to `POST {vsaApiEndpoint}/assistant/chat` with a CMS Cognito bearer token (IdToken) and payload `{ prompt, sessionId, vin? }`.
2. API Gateway invokes the `vsa-api` Cognito authorizer, which validates the IdToken against `EXTRA_USER_POOL_IDS` (the CMS user pool is pre-trusted) and injects claims into the route Lambda context.
3. Route Lambda `vsa-staging-api-assistant-chat` reads `cognito:groups` / `custom:role` from the authorizer context, calls `map_persona_from_claims()` in `lambdas/api-assistant-chat/persona_map.py` to infer `personaId` server-side (default `fleet_driver`), and never trusts a client-supplied persona.
4. The route Lambda invokes `bedrock-agentcore:InvokeAgentRuntime` on the CVX text runtime `vsa_supervisor_text_staging` (us-west-2), passing `{ prompt, sessionId, personaId, vin }` in the payload.
5. On session-init (first turn of a new `sessionId`), the runtime calls `persona_loader.load_persona(persona_id, vehicle_id, customer_id)` which issues a cross-region Athena query (us-west-2 → us-east-1) against the ADP `adp_staging_*` Glue catalog via the Lake Formation v4 cross-account share and writes the results into the AgentCore Memory resource `vsa-staging-cvx-foundation-memory` (us-west-2) with metadata `{persona_id, vehicle_id, customer_id, data_source, tenant_id, last_updated}`.
6. The supervisor agent (Claude Sonnet 4.6, in-container) receives the prompt and dispatches to persona-scoped tools: tabular tools (`vehicle_identity`, `service_history`, `charging_history`, `customer_profile`, `interaction_history`, `ota_history`) call `bedrock-agentcore-control:RetrieveMemoryRecords` with metadata filters against AgentCore Memory; reference tools (`retrieve`, `parts_lookup`, `knowledge`, `diy_repair_advisor`) call `bedrock:Retrieve` against the ADP Bedrock Knowledge Base `adp-staging-vehicle-knowledge` (KB ID `YULHRAUMNU`) cross-account and cross-region via the KB resource policy; local-rule tools (`triage`, `escalate`, `find_service_center`) read local catalogs; write tools (`book`) write to CMS DynamoDB in us-west-2.
7. The supervisor synthesizes a grounded answer with optional citations and the runtime returns `{ result, sessionId, classification?, vin?, citations? }` to the route Lambda.
8. The route Lambda serializes the JSON response back through API Gateway to the browser, where `ChatAgent.tsx` renders the answer through the attribute-stripping `sanitizeHtml` allowlist and displays it in the CMS web UI.

---

## Voice/bidi path (iOS VSACompanion → Nova Sonic)

Applies to the CMS iOS companion app's voice surface. Authoritative for the `vsa_supervisor_bidi_staging` AgentCore bidi runtime. Session-init (step 4) and tool dispatch (step 5) are identical to the web-chat path — only the client, transport, and audio codec tier differ.

1. The iOS `VSACompanion` app authenticates the driver via CMS Cognito, opens a bidirectional stream to `bedrock-agentcore:InvokeAgentRuntimeWithBidirectionalStream` on `vsa_supervisor_bidi_staging` (us-west-2), and begins streaming captured microphone audio frames.
2. The AgentCore bidi runtime accepts the stream, extracts the JWT from the payload (AgentCore HTTP unary does not forward custom headers), and validates the caller and `sessionId`.
3. The runtime container reads `NOVA_SONIC_REGION=us-east-1` and forwards the audio stream to Amazon Bedrock Nova Sonic v2 (`amazon.nova-2-sonic-v1:0`) cross-region via `InvokeModelWithBidirectionalStream`, because Nova Sonic is not yet available in us-west-2.
4. On session-init (first turn), the runtime calls `persona_loader.load_persona(...)` which issues a cross-region Athena query against the ADP `adp_staging_*` Glue catalog and writes tabular data into AgentCore Memory `vsa-staging-cvx-foundation-memory` with per-persona metadata — same session-init as the web-chat path.
5. Nova Sonic streams transcribed user turns into the supervisor agent, which dispatches the same tool set as the web-chat path (Memory-backed tabular tools, KB-backed reference tools, local-rule tools, CMS-write tools) and receives grounded tool results.
6. The supervisor synthesizes the response text and streams it back into Nova Sonic, which generates output audio and streams `{audio_frames, transcript_deltas, tool_use_events}` back over the bidi channel to the runtime.
7. The AgentCore runtime relays the bidi frames to the iOS client over the open stream.
8. `VSACompanion` plays the audio frames through the device speaker while rendering the transcript deltas in the on-screen conversation view.

---

## Notes on divergence and reuse

- **Runtime split**: web-chat targets the text runtime (`vsa_supervisor_text_staging`, HTTP unary invoke); voice/bidi targets the bidi runtime (`vsa_supervisor_bidi_staging`, bidirectional stream invoke). Both runtimes package the same `agents/supervisor/` source tree and share the AgentCore execution role `vsa-staging-agentcore-execution-us-west-2`; only the entrypoint and streaming contract differ.
- **Session-init cost**: `persona_loader.load_persona` runs once per new `sessionId` (measured 6–7s on cold call, tracked as backlog row `Persona-loader latency`) and its output persists in AgentCore Memory for the session lifetime with a 15-minute TTL + lazy-refresh window per the Memory schema in `ARCHITECTURE.md`.
- **Cross-account topology**: staging deploys CVX and ADP into the same account (123456789012) across two regions (CVX us-west-2, ADP us-east-1). The Lake Formation v4 share, the Bedrock KB resource policy, and the Athena workgroup are already wired for cross-account customer deploys — the code path is identical, only the ARNs and IAM trust boundaries change.
- **Graceful degradation**: if AgentCore Memory returns no records or the ADP KB is unavailable, tools return structured "not yet available" messages rather than failing the turn — the response tier still completes, just with reduced grounding.
