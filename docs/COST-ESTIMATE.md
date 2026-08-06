# Cost Estimate

_You are responsible for the cost of the AWS services used while running this Guidance. The sample table below establishes a **monthly baseline for the CVX staging deployment** at the stated workload/scale. CVX is a Bedrock- and AgentCore-heavy workload — most line items are **usage-dependent** (Bedrock model invocation, AgentCore Runtime invocation, AgentCore Memory storage+operations, Nova Sonic bidi voice minutes, cross-region Athena data scanned). Re-verify against current AWS pricing pages before committing to a deployment. Prices are subject to change._

_We recommend creating a [Budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html) through [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/) to help manage costs. For full details, refer to the pricing webpage for each AWS service used in this Guidance. Pricing figures below are approximate list-price at the time of writing (mid-2026) and use US East (N. Virginia) / US West (Oregon) list rates unless otherwise noted._

## Baseline Assumptions

The cost table represents CVX's **staging deployment scale** — a single-tenant, single-account demo + eval workload that exercises every persona and every grounding surface. Adjust the usage-dependent line items proportionally for higher-scale customer deployments.

| Assumption | Value | Source |
|---|---|---|
| AWS account topology | Single account, two regions (CVX `us-west-2`, ADP `us-east-1`) | [`docs/ARCHITECTURE.md` — Cross-Account Topology](./ARCHITECTURE.md#cross-account-topology) |
| Personas seeded | 6 (`fleet_driver`, `consumer`, `retail_advisor`, `vehicle_owner`, `service_advisor`, `technician`) | [`docs/persona-mapping.md`](./persona-mapping.md), [`agents/supervisor/persona_definitions.py`](../agents/supervisor/persona_definitions.py) |
| Vehicles referenced | ~100 in ADP Foundation staging (fleet-composition + service history + KPIs) | [`docs/ARCHITECTURE.md` — Persona grounding](./ARCHITECTURE.md#cvx-on-foundation-v1--persona-grounding) |
| Text chat turns | 1,000/day (~30,000/month) via `/assistant/chat` (CMS UI + demo + UAT) | [`docs/FLOW-STEPS.md` — Web-chat path](./FLOW-STEPS.md#web-chat-path-cms-ui--assistantchat) |
| Text sessions | ~500 unique/day (~15,000/month), ≈2 turns per session | Derived from turn volume + typical CMS UI session pattern |
| Voice/bidi sessions | 100/day (~3,000/month) via iOS `VSACompanion` + Tier 3 evals | [`docs/DEPLOYMENT.md` — Per-persona Tier-3 smoke](./DEPLOYMENT.md#per-persona-tier-3-smoke-audio-staging-only) |
| Voice session duration | ~8 min avg | [`docs/ARCHITECTURE.md` — Memory Schema capacity notes](./ARCHITECTURE.md#agentcore-memory-schema) |
| Voice-minutes/month | ~24,000 min | 3,000 sessions × 8 min |
| Session-init cost | 1 `persona_loader.load_persona` per new session (~18,000/month) issuing 10–15 cross-region Athena queries scanning ~10–20 MB each | [`docs/FLOW-STEPS.md` — Notes on divergence and reuse](./FLOW-STEPS.md#notes-on-divergence-and-reuse), [`docs/ARCHITECTURE.md` — Data Sources v1 Update](./ARCHITECTURE.md#data-sources--v1-update) |
| KB Retrieve calls | ~21,000/month against ADP KB `YULHRAUMNU` (cross-account, cross-region) | Derived: ~0.5 refs/text turn + ~2 refs/voice session |

## Sample Cost Table

The table separates **fixed** line items (deploy-time, incurred regardless of usage), **usage-dependent** line items (billed per invocation / per token / per minute at the stated workload above), and **variable** line items (workload-driven; call out the ranges but do not include in the baseline total).

| AWS service | Dimensions | Cost [USD] |
| ----------- | ------------ | ------------ |
| Amazon API Gateway (REST) | ~30,000 `/assistant/chat` calls/month + eval + smoke-test traffic (~50,000 total requests) | ~$0.20 |
| AWS Lambda (`vsa-staging-api-assistant-chat` + supporting handlers) | ~50,000 invocations/month at 256 MB, avg 200 ms — see [Lambda pricing](https://aws.amazon.com/lambda/pricing/) | ~$0.05 |
| Amazon Cognito (`vsa-api` authorizer, CMS pool trust via `EXTRA_USER_POOL_IDS`) | Included in CMS user pool cost — CVX-side charge zero | $0.00 |
| Amazon CloudWatch (Logs + Transaction Search) | ~5 GB ingested + ~20 GB stored across runtime + Lambda + evals — see [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) | ~$3.00 |
| AWS KMS | Per-stage CMK (decrypt calls for AgentCore Memory + CMS DDB access) | ~$1.00 |
| Amazon S3 (Athena query results bucket, us-east-1) | <10 GB storage, ~30,000 PUT/GET requests/month | ~$0.30 |
| **Fixed / near-fixed subtotal** | | **~$4.55** |
| Amazon Bedrock — Claude Sonnet 4.6 (supervisor agent) | ~30,000 text turns + ~9,000 voice-triggered tool turns. Est. ~120M input tokens + ~30M output tokens/month via cross-region inference profile `us.anthropic.claude-sonnet-4-6` — see [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) | ~$700–$900 |
| Amazon Bedrock — Nova Sonic v2 (`amazon.nova-2-sonic-v1:0`, bidi voice) | ~24,000 voice-minutes/month streamed cross-region us-west-2 → us-east-1 via `InvokeModelWithBidirectionalStream` — see [Bedrock pricing (Nova Sonic)](https://aws.amazon.com/bedrock/pricing/) | ~$400–$700 |
| Amazon Bedrock AgentCore Runtime — `vsa_supervisor_text_staging` + `vsa_supervisor_bidi_staging` | ~15,000 text-runtime invocations/month + ~3,000 bidi sessions/month at ~8 min each. Runtime is billed on per-invocation vCPU-seconds and memory-GB-seconds — see [AgentCore Runtime pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) | ~$200–$400 |
| Amazon Bedrock AgentCore Memory (`vsa-staging-cvx-foundation-memory`) | ~18,000 session-init writes/month (`CreateEvent` / `BatchCreateMemoryRecords`) + ~500,000 tool-driven `RetrieveMemoryRecords`/month; <1 GiB memory storage. Charged on ops + storage — see [AgentCore Memory pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) | ~$50–$150 |
| Amazon Bedrock AgentCore Identity | Token vault + AgentCore-runtime identity operations — usage-based, negligible at staging scale | ~$5–$20 |
| Amazon Bedrock Knowledge Base — Retrieve (ADP `YULHRAUMNU`, cross-account) | ~21,000 `bedrock:Retrieve` calls/month against the ADP-side KB (embedding + retrieval costs invoiced against the ADP KB owner account, not CVX — see [Bedrock Knowledge Base pricing](https://aws.amazon.com/bedrock/knowledge-bases/pricing/)) | ~$5–$30 (CVX-side; ADP-side billed separately) |
| Amazon Athena (persona-loader session-init, cross-region us-west-2 → us-east-1) | ~200–300 GB scanned/month at $5/TB — see [Athena pricing](https://aws.amazon.com/athena/pricing/) | ~$1.50 |
| Cross-region data transfer (us-west-2 ↔ us-east-1) | Athena result payloads + Nova Sonic bidi audio streaming (~90 GB/month) at $0.02/GB — see [EC2 pricing (data transfer)](https://aws.amazon.com/ec2/pricing/on-demand/) | ~$2.00 |
| **Usage-dependent subtotal (at stated workload)** | | **~$1,360–$2,200** |
| **Monthly estimate — staging workload** | | **~$1,400–$2,200** |
| Amazon Bedrock Guardrails (`vsa-vehicle-scope` in us-east-1) | Prod-only in v1 — staging skips guardrails entirely per [`.kiro/specs/2026-05-28-agentcore-staging-infra/decisions.md`](../.kiro/specs/2026-05-28-agentcore-staging-infra/decisions.md); enable for prod at ~$0.75 per 1,000 text units — see [Bedrock Guardrails pricing](https://aws.amazon.com/bedrock/pricing/) | Variable (0 in staging) |
| Amazon SageMaker AI Studio (optional, iOS Xcode dev/UAT only) | Not required for the CVX deployment | Variable (0 baseline) |
| CMS integration (DynamoDB writes for `book` tool, KMS decrypt) | Invoiced against the CMS account; CVX-side write cost <$0.10/month | Variable |

## What Drives Cost

Because CVX is a Bedrock-first, AgentCore-first workload, the cost curve tracks two variables far more than any others:

1. **Bedrock Sonnet 4.6 token volume**, dominated by the supervisor agent's synthesis pass on every text turn and every voice tool-use dispatch. Session-init `persona_loader` output can be up to 100 KiB (per the [AgentCore Memory capacity notes](./ARCHITECTURE.md#agentcore-memory-schema)) which becomes context tokens on the first turn — the largest single per-session token expenditure.
2. **Nova Sonic voice-minutes**, dominated by the average voice session duration (~8 min in staging). Doubling the average session length roughly doubles the Nova Sonic line item; shortening sessions or capping session duration is the single most direct voice cost lever.

Line items below Bedrock/AgentCore (Lambda, API Gateway, CloudWatch, Athena, cross-region data transfer) are trivial by comparison at staging scale — they do not warrant optimization until customer scale is 10–100× the assumed workload.

## Assumptions & Caveats

- The estimate uses **cross-region inference-profile** pricing for Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`); direct foundation-model invocation without a profile is priced identically per-token in supported regions.
- The estimate assumes **no `bedrock:ApplyGuardrail` calls in staging**, matching the CDK-managed staging IAM role which does not grant that action ([`.kiro/specs/2026-05-28-agentcore-staging-infra/decisions.md`](../.kiro/specs/2026-05-28-agentcore-staging-infra/decisions.md)). Prod deployments that enable the `vsa-vehicle-scope` guardrail should add a variable line item.
- The **ADP-side** cost of the cross-account KB Retrieve (embedding storage, OpenSearch Serverless collection, KB ingestion jobs) is invoiced against the ADP account owner, not the CVX account. See ADP's cost documentation for that side of the topology.
- The **Tier 3 eval pipeline** (`evals/runner/tier3_e2e.py`) generates a portion of the voice load — approximately $1/run per [`docs/EVAL.md`](./EVAL.md). Tier 1 and Tier 2 are free ($0/run — mocked Bedrock).
- **Nova Sonic regional gap:** Nova Sonic is not available in `us-west-2` at the time of writing; the runtime routes bidi calls cross-region to `us-east-1`. If Nova Sonic becomes available in `us-west-2`, the cross-region data-transfer line item drops to near zero.
- The **`persona_loader` session-init** cost includes a first-turn latency of ~6–7 seconds (tracked as backlog row `Persona-loader latency`); this is a latency budget item, not a direct cost driver, but does inflate AgentCore Runtime CPU-seconds on first-turn invocations.

## Well-Architected Alignment

For the Cost Optimization pillar rationale behind these choices (usage-based pricing, serverless proxy, evals-guarded deploys), see [`WELL_ARCHITECTED.md`](../WELL_ARCHITECTED.md#cost-optimization).
