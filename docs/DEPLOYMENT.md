# AVX Deployment Guide

Agentic Vehicle Experience (AVX) — deployment runbook.

AVX has two independently-deployable components: **CDK infrastructure** (Lambdas, API Gateway, DynamoDB, Cognito) and the **AgentCore runtime** (`vsa_supervisor` text agent + `vsa_supervisor_bidi` voice agent). Most code changes touch one or the other, not both. Knowing which lets you avoid unnecessary 5-minute redeploys.

## Prereqs

### Accounts & regions

| Item | Value |
|------|-------|
| AWS account | `123456789012` |
| Region | `us-east-1` (production default; AgentCore pinned here) |
| Bedrock model access | Claude Sonnet + Nova Sonic v2 enabled in `us-east-1` |

### Tools

- AWS CLI v2 with credentials for the account above
- Python 3.12 with `.venv` activated (`python3 -m venv .venv && source .venv/bin/activate`)
- Node.js 20 (for CDK)
- `bedrock-agentcore` SDK installed (`pip install bedrock-agentcore-starter-toolkit` — provides the `agentcore` CLI)

### Upstream dependencies

AVX depends on two other deployments existing first:

- **CMS** ([`guidance-for-connected-mobility-on-aws`](https://github.com/aws-solutions-library-samples/guidance-for-connected-mobility-on-aws)) — provides DynamoDB tables, Redis cluster, KMS keys
- **ADP** ([`guidance-for-automotive-data-platform-on-aws`](https://github.com/aws-solutions-library-samples/guidance-for-automotive-data-platform-on-aws), sub-guidance `guidance-for-vehicle-knowledge-base`) — provides Bedrock Knowledge Base for grounded answers (optional; AVX falls back to local `corpora/` if absent)

If CMS or ADP isn't deployed, see their respective READMEs / DEPLOYMENT.md.

### Configuration files

- `.env` — populated from `.env.example`, contains CMS table names, KMS ARN, Redis endpoint, Cognito IDs (post-deploy)
- `.bedrock_agentcore.yaml` — populated from `.bedrock_agentcore.yaml.example`, contains AgentCore execution role ARN + S3 bucket

Both files are gitignored. Both must exist before any deploy command will succeed.

## Per-Stage Parameters (REQUIRED before any deploy)

Every per-stage infrastructure coordinate — KMS key ARNs, the Redis endpoint, VPC and security
group IDs, Cognito pool IDs, the AgentCore runtime ARN — lives in SSM Parameter Store under
`/cvx/{stage}/`. None of it is baked into `infrastructure/bin/vsa.ts` any more. That file is the
CDK app entrypoint, so it cannot be excluded from a public publish, which made it the worst place
to keep live account coordinates. See spec `2026-08-05-cvx-stage-defaults-parameterization`.

**A stage cannot be deployed until its parameters exist.** Seed them once per stage:

```bash
# Dry run is the default — shows what would be written, writes nothing.
bash scripts/seed-stage-parameters.sh --stage staging
bash scripts/seed-stage-parameters.sh --stage staging --apply

# Names only, values never printed.
bash scripts/seed-stage-parameters.sh --stage staging --verify
```

The script recovers each value from a git ref that still had it baked (`--from-ref`, default
`HEAD`). It never prints a value, is idempotent, and exits non-zero listing any gaps rather than
seeding partially.

**Prod has one gap by design.** `cmsKmsKeyArn` was never baked for prod — it came from `.env`
or CI secrets specifically to keep the prod CMS KMS ARN out of git. So it cannot be recovered and
must be supplied:

```bash
CMS_KMS_KEY_ARN='<prod-cms-kms-key-arn>' \
  bash scripts/seed-stage-parameters.sh --stage prod --apply
```

### The parameters

`/cvx/{stage}/` — 14 per stage, plus the optional `tier2-alarm-email`. Regions: staging `us-west-2`, prod `us-east-1`.
<!-- verify: grep -cE '^\s+"[a-zA-Z]+:[a-z0-9-]+:[A-Z0-9_]+:(A|B)"' scripts/seed-stage-parameters.sh -->

| Parameter | Purpose | Tier |
|---|---|---|
| `cms-kms-key-arn` | CMS DynamoDB SSE-KMS key; IAM decrypt grants | A |
| `redis-endpoint` | Vehicle live-state cache endpoint (Lambda env var) | A |
| `assistant-chat-runtime-arn` | AgentCore text runtime invoked by `/assistant/chat` | A |
| `dms-api-endpoint` | DMS REST API base URL for the AVX approve route's DMS executor | A |
| `connect-instance-arn` | ARN of the one Amazon Connect instance the P0 outbound callback may dial from; scopes `connect:StartOutboundVoiceContact` on the escalate role to `<this ARN>/contact/*`. No git source: take it from the tenant config's `channels.connect.instanceArn`. Override: `CVX_CONNECT_INSTANCE_ARN` | A |
| `avx-fleet-ids-claim-trusted` | `true`/`false`: operator attestation that `custom:fleetIds` may grant AVX fleet standing. Seed `true` only after `bash scripts/check-standing-claim-pools.sh --stage <stage>` prints `RESULT: PASS` (contract § 5.1: every pool the authorizer trusts keeps standing claims out of client `WriteAttributes`); there is no git source, so it must be supplied (`CVX_AVX_FLEET_IDS_CLAIM_TRUSTED=true`). Staging: `true`. | A |
| `avx-customer-id-claim-trusted` | `true`/`false`: operator attestation that `custom:customerId` may grant AVX owner standing. Threaded to the findings Lambda ONLY (not actions). Seed `true` only after the CMS pool declares `custom:customerId` and `check-standing-claim-pools.sh` passes. Override: `CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED`. Staging: `true` since 2026-09-24. | A |
| `avx-vehicle-id-claim-trusted` | `true`/`false`: operator attestation that `custom:vehicleId` may grant AVX driver standing. Threaded to the findings Lambda ONLY (not actions). Seed `true` only after `check-standing-claim-pools.sh` passes; it covers the CVX fleet pool, whose `custom:vehicleId` was client-writable until its clients were restricted to `["email","name"]` on 2026-09-24. Override: `CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED`. Staging: `true` since 2026-09-24. | A |
| `memory-kms-key-arn` | AgentCore Memory CMK (Foundation stack output) | B |
| `vpc-id` | CMS VPC the Redis-backed Lambdas join | B |
| `redis-security-group-id` | SG granting the live-state Lambda access to Redis | B |
| `voice-auth-user-pool-id` | Shared CMS UI pool trusted for the voice credential exchange | B |
| `voice-auth-user-pool-client-id` | That pool's app client | B |
| `extra-cognito-user-pool-ids` | Extra pools the API's Cognito authorizer must accept | B |

**Tier A** is read by CloudFormation at deploy time via
`AWS::SSM::Parameter::Value<String>`. Nothing extra is needed — but a missing parameter fails the
stack operation, so seed before deploying.

**Tier B** must be a concrete literal at synth time (`Vpc.fromLookup` rejects tokens; a
comma-separated pool list becomes N constructs; three IAM/trust decisions are synth-time
conditionals). `scripts/with-stage-env.sh` reads these from SSM and exports them, and the
`make` targets already wrap every CDK invocation in it — so in normal use an operator sets
nothing. It prints a one-line summary such as `tier-B: 6 from SSM, 0 already exported, 0 missing`.
**If that line reports anything missing, stop** — the deploy is about to use a value you did not
intend.

### Overrides

An explicitly exported env var always wins over SSM, for every field:

| Field | Env var | `-c` context key |
|---|---|---|
| `cmsKmsKeyArn` | `CMS_KMS_KEY_ARN` | — |
| `redisEndpoint` | `REDIS_ENDPOINT` | — |
| `assistantChatRuntimeArn` | `AVX_RUNTIME_ARN` | `cvxRuntimeArn` |
| `memoryKmsKeyArn` | `VSA_MEMORY_KMS_KEY_ARN` | `memoryKmsKeyArn` |
| `vpcId` | `CMS_VPC_ID` | `vpcId` |
| `redisSecurityGroupId` | `CMS_REDIS_SG_ID` | `redisSecurityGroupId` |
| `voiceAuthUserPoolId` | `VOICE_AUTH_USER_POOL_ID` | `voiceAuthUserPoolId` |
| `voiceAuthUserPoolClientId` | `VOICE_AUTH_USER_POOL_CLIENT_ID` | `voiceAuthUserPoolClientId` |
| `extraCognitoUserPoolIds` | `EXTRA_USER_POOL_IDS` | `extraCognitoUserPoolIds` |

### What a missing value looks like

Synth fails, naming the field, the env var, the parameter path and the opt-out:

```
Error: memoryKmsKeyArn is required for stage "staging" and was not supplied.

Supply it one of two ways:
  • export VSA_MEMORY_KMS_KEY_ARN=<value>
  • pass -c memoryKmsKeyArn=<value> to the cdk command

The canonical value for this stage is recorded in SSM at /cvx/staging/memory-kms-key-arn:
  aws ssm get-parameter --name /cvx/staging/memory-kms-key-arn --query Parameter.Value --output text
```

This is deliberately fail-closed. Nothing falls back to an empty string, because an empty
coordinate used to produce a deployment that looked successful and then misbehaved at runtime.

**Opting out.** A stage that genuinely lacks a coordinate sets `<VAR>=none`, which records the
choice rather than leaving it unset. Only `memoryKmsKeyArn`, `voiceAuthUserPoolId` and
`voiceAuthUserPoolClientId` accept it. `vpcId` and `redisSecurityGroupId` reject it — they have no
valid absent case.

> ⚠️ **`extraCognitoUserPoolIds` is different, and the difference is deliberate.** It requires
> `EXTRA_USER_POOL_IDS=cvx-only-trust`, not `none`. This field decides whether the CMS web UI's
> Cognito pool is trusted by the VSA API's authorizer, and **omitting it does not fail loudly — it
> returns 401 on the `/assistant/chat` route with no other symptom.** That landmine was caught live
> during the 2026-07-13 CDK-cleanup deploy. Opting out has to be an unambiguous act rather than the
> same word used for every other optional field, so `none` is rejected here with an error.
>
> After any deploy that could touch the authorizer, verify the trust survived — this is the
> regression check, and a green deploy is not a substitute for it:
>
> ```bash
> API_ID=$(aws apigateway get-rest-apis --region <region> \
>   --query "items[?name=='vsa-<stage>-api'].id" --output text)
> aws apigateway get-authorizers --rest-api-id "$API_ID" --region <region> \
>   --query 'sort(items[].providerARNs[])' --output json | shasum -a 256
> ```
>
> Compare the digest before and after. A digest rather than the raw ARNs, so no pool ID lands in a
> terminal scrollback or CI log.

### IAM: scope the read to `/cvx/{stage}/*`

These parameters are `String`, **not** `SecureString` — CloudFormation's
`AWS::SSM::Parameter::Value<String>` type does not support `SecureString`, and that type is the
whole Tier A mechanism. So anyone holding `ssm:GetParameter` on a broad path can read them. They
are infrastructure coordinates rather than credentials, but they are exactly the coordinates this
project stopped publishing, so **do not grant `ssm:GetParameter*` on `parameter/*`** to deploy or
CI roles. Scope it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "CvxStageParametersRead",
    "Effect": "Allow",
    "Action": ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
    "Resource": "arn:aws:ssm:<region>:<account-id>:parameter/cvx/<stage>/*"
  }]
}
```

Seeding additionally needs `ssm:PutParameter` on the same path, and that belongs to an operator,
not to a deploy role. No AVX stack grants itself any SSM permission — CloudFormation resolves the
parameters using the *deploying* principal's credentials, so the scoping above is the only place
this access is controlled.

## Deploy Commands

### Component 1: CDK infrastructure

Run when CDK code under `infrastructure/`, `lambdas/`, or shared Lambda code changes.

```bash
# 1. Build Lambda assets (each script bundles deps into the Lambda's source dir)
scripts/build-triage-classifier.sh
scripts/build-api-ws-authorizer.sh
scripts/build-api-vehicle-live-state.sh

# 2. Synth + deploy
source .env  # loads CMS_STAGE, AWS_REGION, account ID, etc.
make deploy  # equivalent to: cd infrastructure && cdk deploy --all --require-approval never
```

Stacks deployed: `vsa-core-stack`, `vsa-api-stack`, `triage-classifier-stack`, `tenant-pool` (one per tenant). Order: CDK handles dependencies.

### Component 1b: CDK + AgentCore staging (us-west-2)

Updated by spec `2026-05-28-agentcore-staging-infra/` (initial CDK staging by `2026-05-28-staging-auto-deploy/`). Staging stacks (`vsa-staging-*`) live in `us-west-2` and depend on CMS resources already provisioned there (DynamoDB tables, Redis cluster, KMS key, VPC, Redis SG).

#### New in v1: Acquire stack (Order + Build journey)

Spec `2026-08-04-cvx-oem-discover-order-meridian` adds three new DynamoDB tables (`vsa-staging-acquire-catalog`, `vsa-staging-acquire-orders`, `vsa-staging-acquire-leads`) deployed via a new `vsa-staging-acquire` CDK stack. These tables back the Order and Build phases of the customer journey (configurator, finance, reservation, order tracker, time-simulated order progression).

**Stack deployment:**

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy vsa-staging-acquire --require-approval never
cd ..
```

**Expected outcome:**
- `vsa-staging-acquire` stack in `CREATE_COMPLETE`
- Three tables created: `vsa-staging-acquire-catalog-<region>-<account>`, `vsa-staging-acquire-orders-<region>-<account>`, `vsa-staging-acquire-leads-<region>-<account>` (region-scoped per cross-region-namespace discipline).

**Seed the GenericMoto catalog:**

```bash
VSA_ACQUIRE_CATALOG_TABLE=vsa-staging-acquire-catalog \
  python3 scripts/seed-acquire-catalog.py \
  --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json \
  --region us-west-2
```

This populates `vsa-staging-acquire-catalog` with 74 items covering 3 categories, 6 models, 12 variants, 18 colors, 8 accessories, and 26 compatibility records — sufficient for a complete Discover + Order + Build walkthrough. For a tenant-specific catalog:

```bash
VSA_ACQUIRE_CATALOG_TABLE=vsa-staging-acquire-catalog \
  python3 scripts/seed-acquire-catalog.py \
  --catalog /tmp/<your-oem>-catalog.json \
  --region us-west-2
```

See `scripts/seed_data/acquire_catalog/README.md` for the catalog JSON schema and how to customize it.

#### New in v1: Web client chat route

Spec `2026-06-19-cvx-web-ui-surfacing` adds a **new REST route** `POST /assistant/chat` on `vsa-api` for the CMS web UI. The route invokes the AVX text agent, infers persona from CMS Cognito claims, and returns grounded answers.

**Route details:**
- **Path:** `POST {vsa-staging-api-url}/assistant/chat`
- **Auth:** Cognito bearer token (CMS IdToken); `vsa-api` authorizer validates against `EXTRA_USER_POOL_IDS` (CMS pool already trusted)
- **Request:** `{ prompt, sessionId, vin? }`
- **Response:** `{ result, sessionId, classification?, vin?, citations? }`
- **Lambda:** `vsa-staging-api-assistant-chat` (deployed as part of `vsa-api-stack`)
- **IAM:** route Lambda role granted `bedrock-agentcore:InvokeAgentRuntime` on the text-mode AVX runtime (same-account grant)

**Deploy:** included in `make deploy-staging` (no separate step required). The CDK infrastructure automatically wires the route, Lambda, and IAM grants.

**Persona mapping:** The route infers `personaId` from CMS claims and passes it to the AVX entrypoint. Valid personas and mapping rules are documented in `docs/persona-mapping.md`.

**CMS-side:** The CMS web UI's `ChatAgent.tsx` now sends requests to this route via `${getVsaApiEndpoint()}/assistant/chat` instead of invoking Bedrock directly. See CMS repo `modules/cms_ui/README.md` for web chat integration notes.

#### Standard staging deploy

`make deploy-staging` now deploys the **full** staging stack — CDK (including the new chat route) + AgentCore voice runtime + iOS xcconfig back-fill — in a single command:

1. CDK deploy (creates `vsa-staging-{core,api,triage-classifier}` stacks, including the new ApiAssistantChat Lambda + route resource).
2. `scripts/patch-agentcore-yaml.py` — reads CFN outputs and patches `.bedrock_agentcore.yaml::agents.vsa_supervisor_bidi_staging.aws.{execution_role,s3_path}`.
3. `agentcore deploy -a vsa_supervisor_bidi_staging --auto-update-on-conflict --env ...` — builds the runtime container via CodeBuild and updates the AgentCore Runtime in `us-west-2`. The Makefile passes **17** `--env` variables (`Makefile:221-239`); `--env` is the only source of runtime environment and is **not** merged with the running config, so an omitted variable is deleted from the runtime. `NOVA_SONIC_REGION` **must equal the execution-role region** — see the callout below. Do not hand-roll the `--env` list; see the warning under "Manual deploy" for why.
4. `scripts/update-staging-xcconfig.py` — reads the deployed runtime ARN from `.bedrock_agentcore.yaml` and patches `VSA_AGENTCORE_RUNTIME_ARN` in the CMS repo's `Staging.xcconfig`. No-op when the CMS repo is not present alongside AVX (e.g., in CI).

#### Session memory (text + voice) — env vars and what they do

> **STATUS 2026-09-22: committed, NOT deployed, NOT live-verified.** Spec
> `2026-09-22-cvx-text-session-memory` Groups 1–3 are on `main` with both review gates PASS, but the
> `bedrock-agentcore:ListEvents` grant below is **not yet on the live role** and the runtime has not
> been redeployed. The present tense in this section describes the committed design, not observed
> behaviour. Until `make deploy-staging` runs, hydration returns `AccessDeniedException` on every
> read — and because `CreateEvent` *is* already granted, that presents as turns being written while
> the assistant stays amnesiac, i.e. **indistinguishable from the original bug**. See the failure-signature
> table further down in this document for how to tell the two apart.

Spec `2026-09-22-cvx-text-session-memory` added server-side conversation-turn persistence: each
completed turn is written to AgentCore Memory and the last K turns are hydrated at the start of the
next invocation, so the assistant stops re-asking for information the user already gave. **Four**
variables govern it. The three *new* ones are currently **ABSENT from the Makefile's `--env` lists**
and run on their code defaults, which are correct for staging — see the caveat below. Note `VSA_TOOL_MEMORY_READS`'s default was changed to `false` on 2026-09-22 on the strength of T4.2's measurement; see its row.
`VSA_FOUNDATION_MEMORY_ID` is the exception: it predates this spec and **is** passed.

| Variable | Default | What it does |
|---|---|---|
| `VSA_FOUNDATION_MEMORY_ID` | *(none — required)* | The AgentCore Memory resource. Empty/absent disables session memory entirely; the runtime degrades to the stateless pre-2026-09-22 behaviour rather than erroring. **Is** passed by `make deploy-staging`. |
| `VSA_MEMORY_REGION` | `us-west-2` | Region of that Memory resource. **NOT passed by the Makefile** — the default coincides with where the staging resource lives. A Memory resource in any other region would silently break hydration. |
| `VSA_MEMORY_HYDRATE_TURNS` | `10` | How many prior turns to replay. Bounded and clamped to a hard maximum of 100, because prior turns cost prompt tokens on **every** turn — unbounded growth is a slow latency regression, not a one-off cost. `<= 0` is treated as 1. |
| `VSA_TOOL_MEMORY_READS` | **`false`** | Gates the 8 `retrieve_memory_records` calls in `agents/supervisor/tools/`. **Defaults OFF as of 2026-09-22** — T4.2 measured each call at **p50 343 ms / p95 516 ms** against a ~1 s per-tool budget, where before the `Config` fix they raised `AttributeError` in ~0 ms. All three voice personas carry 3–6 of these tools. Off returns each tool's existing degraded payload **without constructing a boto3 client** — i.e. today's behaviour minus the latency, since these calls have failed on every invocation since 2026-06-18. Set to `true` to switch the reads on; the parser fails closed, so only `1\|true\|on\|yes` enables. **Hydration is independent of this flag** and stays on either way. |

**Why `VSA_TOOL_MEMORY_READS` exists, and why it now defaults OFF.** Adding `memory_id` to `Config`
switched on 8 memory reads that had been failing fast with an `AttributeError` since 2026-06-18. Those
same tools serve the **voice** path, whose Nova Sonic bidi loop assumes tool returns inside ~1 s. T4.2
measured the cost directly against the live staging Memory resource, mirroring
`tools/vehicle_identity.py`'s exact call:

```
boto3.client("bedrock-agentcore")   warm p50   1.6 ms   (built PER tool call)
retrieve_memory_records             p50  341.5 ms | p95 514.7 ms
-----------------------------------------------------------------
per gated tool call                 p50  343.1 ms | p95 516.4 ms
```

That is ~34–52% of the per-call budget, from a prior cost of ~0 ms — and it is the **floor**, since
both measured namespaces returned 0 records and these are semantic searches, not key lookups. So the
flag ships **off**: the reads stay gated until someone decides the persona enrichment is worth
343 ms, with numbers in hand. Off is today's behaviour minus the latency, because these calls have
never once succeeded.

**Turning the reads ON requires adding the variable to the `--env` list.** `agentcore deploy --env` is
the only source of runtime environment and is **not** merged with the running config, so a variable
absent from the list is absent from the runtime — which is why the safe state is the *code* default
rather than a deploy-time value. One line per runtime block:

```make
    --env VSA_TOOL_MEMORY_READS=$${VSA_TOOL_MEMORY_READS:-false} \
```

Adding that line is worthwhile for operability even while the default stays `false`: it makes the gate
**visible** when inspecting a deployed runtime's environment, instead of being inferable only from the
code. It is not required for correctness.

**Do not hand-roll an `agentcore deploy --env …` to set it.** The text runtime's list is **18** `--env`
flags (20 variables end up on the runtime; the toolkit injects two
`BEDROCK_AGENTCORE_MEMORY_*` of its own) and omitting any one deletes it from the runtime — a
recurring footgun where a partial env list silently drops variables the code depends on.

**The IAM grant hydration depends on.** `get_last_k_turns` calls `ListEvents` and nothing else, so
the runtime role needs `bedrock-agentcore:ListEvents` (added to `CvxAgentCoreMemoryOps` in
`infrastructure/lib/vsa-core-stack.ts`). Without it, writes succeed and every read returns
`AccessDeniedException`, which the caller degrades to an empty history — so the assistant appears
**exactly as amnesiac as before the fix** while a durable store of turns accumulates. Verify with:

```bash
# Enumerate ALL inline policies rather than naming one — the CDK-synthesised
# policy name (…DefaultPolicyEC87D1B7) changes on refactor, and a hardcoded name
# would make this return 0 for the wrong reason: indistinguishable from
# "not deployed", which is the exact confusion the STATUS block above warns about.
ROLE=AmazonBedrockAgentCoreSDKRuntime-us-west-2-staging
for P in $(aws iam list-role-policies --role-name "$ROLE" --region us-west-2 \
             --query 'PolicyNames[]' --output text); do
  aws iam get-role-policy --role-name "$ROLE" --policy-name "$P" \
    --region us-west-2 --query 'PolicyDocument' --output json
done | grep -c 'bedrock-agentcore:ListEvents'
# must print 1 once deployed. As of 2026-09-22 it prints 0 — see the STATUS note above.
```

The verified Memory API contract (return shapes, role casing, the mixed turn/message ordering)
is captured inline in `agents/supervisor/memory.py` and its test suite. To verify continuity
end-to-end, exercise a two-turn conversation against the deployed runtime and confirm the second
turn's Memory `ListEvents` returns the first turn's `USER`+`ASSISTANT` payload.


**Manual deploy (with ADP KB):**

The staging deploy requires `VSA_ADP_KB_ID` to wire cross-account Bedrock KB access. The KB ID is passed as a CDK context variable (`-c adpKbId=`) to tighten the IAM grant to that specific knowledge base (spec `2026-06-18-cvx-adp-kb-wireup`):

```bash
# 1. Deploy CDK stacks with KB context (tightens IAM policy to specific KB ARN)
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy vsa-staging-core -c adpKbId=YULHRAUMNU --require-approval never
cd ..

# 2. Deploy AgentCore runtime with KB env vars
VSA_ADP_KB_ID=YULHRAUMNU make deploy-staging
```

The Makefile pre-flight assertion enforces `VSA_ADP_KB_ID` — deploy will fail fast if empty. Optional env vars: `VSA_ADP_REGION` (default `us-east-1`), `VSA_ADP_ACCOUNT_ID` (default resolved via STS `get-caller-identity`).

**Auto-deploy** (preferred path): every push to `main` triggers `.github/workflows/evals.yml`. After Tier 1/2/3 evals pass, the `deploy-staging` job runs `make deploy-staging` against the staging account via OIDC (`secrets.AWS_ROLE_ARN`). PR pushes do NOT deploy.

**Staging environment reference:**

| Item | Value |
|------|-------|
| Region | `us-west-2` |
| Account | `123456789012` |
| CMS stage | `staging` |
| CMS KMS key | `arn:aws:kms:us-west-2:<account>:key/<cms-kms-key-uuid>` |
| Redis endpoint | `<redis-primary-endpoint>` |
| VPC | `<vpc-id>` |
| Redis SG | `<redis-security-group-id>` |
| AgentCore execution role | `vsa-staging-agentcore-execution-us-west-2` (CDK-managed) |
| AgentCore S3 bucket | `vsa-staging-agentcore-codebuild-123456789012-us-west-2` (CDK-managed) |
| AgentCore runtime | `vsa_supervisor_bidi_staging` agent in `.bedrock_agentcore.yaml` |

The placeholdered rows above are real per-account coordinates, deliberately not published. They now live in SSM under `/cvx/staging/` — see § "Per-Stage Parameters" above for the full list, how to seed them, and the IAM scoping. Read a value with `aws ssm get-parameter --name /cvx/staging/<param> --query Parameter.Value --output text`, or substitute your own CMS deployment's values. Env-var overrides (`CMS_VPC_ID`, `CMS_REDIS_SG_ID`, `CMS_KMS_KEY_ARN`, `REDIS_ENDPOINT`) take precedence if set. They are no longer in `STAGE_DEFAULTS` in `infrastructure/bin/vsa.ts`, which now holds only the stage and region strings.

> Placeholders rather than literals because **this file is meant to ship to a public mirror unchanged.** Every literal account ID, KMS key UUID, VPC ID, and pool ID belongs in SSM, not here.

**Nova Sonic region MUST match the execution-role region (do not set this cross-region):** Amazon Nova Sonic v2 (`amazon.nova-2-sonic-v1:0`) **is** available in `us-west-2` (verified `ACTIVE`, `responseStreamingSupported: true`, ON_DEMAND — re-confirmed 2026-09-21 via `aws bedrock get-foundation-model`), alongside `us-east-1`, `eu-north-1`, `ap-northeast-1`. Staging runs **native** `NOVA_SONIC_REGION=us-west-2`, which is what `make deploy-staging` sets.

Setting this cross-region is **known-broken, not merely suboptimal**. `InvokeModelWithBidirectionalStream` enforces an undocumented service-side allowlist on the *execution-role name* (`AmazonBedrockAgentCoreSDKRuntime-*`), and a `us-west-2` role invoking Nova Sonic in `us-east-1` fails with an **empty-message `AccessDeniedException`**. That failure is close to undiagnosable from the usual signals: `simulate-principal-policy` returns `allowed` and `get-foundation-model-availability` returns `AUTHORIZED`/`AVAILABLE`. See the inline note at `Makefile:218` for the fix.

Historical context: earlier releases of this guidance described a "Nova Sonic regional gap (HIGH RISK)" and prescribed `NOVA_SONIC_REGION=us-east-1` with a cross-region IAM grant. That guidance was superseded by both the role-name fix and AWS expanding regional availability. Do not re-introduce a cross-region setup.

**iOS endpoint back-fill** (partial automation, partially manual): `make deploy-staging` automatically back-fills `VSA_AGENTCORE_RUNTIME_ARN` in the paired Connected Mobility repository's `clients/ios/VSACompanion/Config/Staging.xcconfig` after the runtime deploys. The remaining six placeholders — REST URL, WS URL, telemetry WS, three Cognito IDs — still require manual back-fill from CFN stack outputs.

```bash
# Capture remaining staging endpoints (REST/WS URLs + Cognito IDs)
aws cloudformation describe-stacks --stack-name vsa-staging-api --region us-west-2 \
  --query 'Stacks[0].Outputs' --output table
aws cloudformation describe-stacks --stack-name vsa-staging-core --region us-west-2 \
  --query 'Stacks[0].Outputs' --output table
```

### Component 1c: CDK + AgentCore prod (us-east-1)

> **Historical record — partly superseded 2026-08-05.** This section documents how the
> 2026-07-16 prod cutover actually ran, including its phase ordering and its
> `AVX_RUNTIME_ARN` placeholder workaround. Two things it describes no longer exist:
> `STAGE_DEFAULTS` no longer holds any ARN or pool ID (they are SSM parameters under
> `/cvx/prod/` — see § "Per-Stage Parameters"), and the synth-time throw on an empty
> `assistantChatRuntimeArn` was **deleted**, because under SSM that value is a
> CloudFormation token, which is never empty, so the check could not fire. Follow
> § "Per-Stage Parameters" for anything you are doing today; read on for what happened.

Shipped via spec `2026-07-16-cvx-prod-cutover` (2026-07-17). Prod stacks (`vsa-prod-*`) live in `us-east-1` — the same region AVX's account-level default already targets, unlike staging's `us-west-2`. Prod colocates the AgentCore text runtime, the Foundation Memory, and the ADP Knowledge Base all in `us-east-1` for latency (same-region `InvokeAgentRuntime` + KB `Retrieve` avoid a cross-region SigV4 hop on every chat turn) — see spec `decisions.md` Q1.

#### The `/assistant/chat` route in prod

Mirrors staging's route exactly (spec `2026-06-19-cvx-web-ui-surfacing` shipped the route to staging; this cutover was the first-time prod enablement — prod's CDK code had `STAGE_DEFAULTS.prod.assistantChatRuntimeArn` deliberately empty with a synth-time guardrail until this cutover filled it in):

- **Path:** `POST {vsa-prod-api-url}/assistant/chat`
- **Auth:** Cognito bearer token (CMS-prod IdToken); `vsa-prod-authz` authorizer validates against both `<cvx-tenant-pool-id>` (AVX per-tenant pool) and `<cms-prod-ui-pool-id>` (CMS-prod-ui pool)
- **Request:** `{ prompt, sessionId, vin? }`
- **Response:** `{ result, sessionId, classification?, vin?, citations? }`
- **Lambda:** `vsa-prod-api-assistant-chat` (deployed as part of `vsa-api-stack`, prod stage)
- **IAM:** route Lambda role granted `bedrock-agentcore:InvokeAgentRuntime` on the prod text-mode runtime ARN (same-account grant)

**CMS-side:** no CMS-repo changes were needed for the prod cutover. CMS-prod's `runtimeConfig.json` already pointed at `https://your-api-id.execute-api.us-east-1.amazonaws.com/prod/` and the ChatAgent widget has never been feature-flag-gated — the widget started working automatically once the route landed.

#### First-time prod deploy sequence (phased, with STOP GATEs)

**⚠️ This was executed as a phased, gated deploy — NOT a single `make deploy`.** Unlike `make deploy-staging`'s single-command wrapper, the prod cutover deliberately ran each stack's `cdk deploy` individually with an explicit `cdk diff` review before each phase, because it was landing accumulated staging-only CDK changes onto stale production resources plus two genuinely first-time-deployed stacks. Follow this same STOP GATE pattern for any future prod-infrastructure change of comparable size: run one `cdk deploy` at a time, present the `cdk diff` for approval before executing, and refuse to proceed to the next phase until the current phase is verified.

**Phase 1 — Foundation (Memory + CMK), net-new, low risk:**

```bash
cd infrastructure
VSA_STAGE=prod AWS_REGION=us-east-1 \
  AVX_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/vsa_supervisor-df1F9yHqDm' \
  npx cdk diff vsa-prod-foundation   # review the diff — STOP GATE 1
VSA_STAGE=prod AWS_REGION=us-east-1 \
  AVX_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/vsa_supervisor-df1F9yHqDm' \
  npx cdk deploy vsa-prod-foundation --require-approval never
cd ..
```

`AVX_RUNTIME_ARN` is a **placeholder** — the app has a synth-time throw if `assistantChatRuntimeArn` is empty, and prod's real runtime doesn't exist yet at this phase. Any legacy runtime ARN works as the placeholder since `vsa-prod-api` (the only stack that actually reads this ARN) isn't deployed until Phase 3. Capture the `MemoryKmsKeyArn` / `MemoryId` CFN outputs and backfill them into `infrastructure/bin/vsa.ts`'s `STAGE_DEFAULTS.prod` before Phase 2A.

**Phase 2A — Core / Analytics / Triage-Classifier, large accumulated diff:**

```bash
cd infrastructure
# ⚠️ REQUIRED undocumented-until-now context flag — see Troubleshooting below.
# Without -c adpStage=prod, vsa-core-stack.ts silently defaults to 'staging'
# and regresses the S3 grants to staging buckets.
VSA_STAGE=prod AWS_REGION=us-east-1 \
  AVX_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/vsa_supervisor-df1F9yHqDm' \
  npx cdk diff vsa-prod-core -c adpStage=prod   # STOP GATE 2, review each of the 3 stacks
VSA_STAGE=prod AWS_REGION=us-east-1 \
  AVX_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/vsa_supervisor-df1F9yHqDm' \
  npx cdk deploy vsa-prod-core -c adpStage=prod --require-approval never

# If a prod ADP Bedrock KB exists (check: aws bedrock-agent list-knowledge-bases),
# add -c adpKbId=<KB_ID> to the core deploy above to wire cross-account Retrieve.

npx cdk deploy vsa-prod-analytics -c adpStage=prod --require-approval never
bash scripts/build-triage-classifier.sh   # rebuild the pre-bundled Lambda asset first — see Troubleshooting
VSA_STAGE=prod AWS_REGION=us-east-1 \
  AVX_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/vsa_supervisor-df1F9yHqDm' \
  npx cdk deploy vsa-prod-triage-classifier -c adpStage=prod --require-approval never
cd ..
```

**Phase 2B — AgentCore text runtime provisioning (mirrors staging's `vsa_supervisor_text_staging` drill exactly):**

```bash
agentcore configure \
  --entrypoint agents/supervisor/agentcore_app.py \
  --name vsa_supervisor_text_prod \
  --execution-role arn:aws:iam::123456789012:role/AmazonBedrockAgentCoreSDKRuntime-us-east-1-prod \
  --region us-east-1 --deployment-type direct_code_deploy --runtime PYTHON_3_12 \
  --protocol HTTP --disable-otel --non-interactive
```

Then patch `.bedrock_agentcore.yaml`'s `vsa_supervisor_text_prod` block (gitignored, local-only edit — `agentcore configure` records absolute paths that break `agentcore deploy`, same pitfall as staging, see Troubleshooting):

```yaml
entrypoint: agents/supervisor/agentcore_app.py   # relative, not absolute
source_path: .                                    # relative, not absolute
aws:
  s3_path: s3://vsa-prod-agentcore-codebuild-123456789012-us-east-1   # the CDK-managed bucket from Phase 2A
  s3_auto_create: false
memory:
  mode: STM_ONLY
  memory_id: <MemoryId from Phase 1 CFN output>
  memory_arn: <MemoryArn from Phase 1 CFN output>
  memory_name: vsa_prod_cvx_foundation_memory
```

```bash
MEMORY_ID=$(aws cloudformation describe-stacks --stack-name vsa-prod-foundation --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`MemoryId`].OutputValue' --output text)

agentcore deploy -a vsa_supervisor_text_prod --auto-update-on-conflict \
  --env CMS_STAGE=prod --env VSA_STAGE=prod --env AWS_REGION=us-east-1 --env VSA_CMS_REGION=us-east-1 \
  --env VSA_FOUNDATION_MEMORY_ID="$MEMORY_ID" \
  --env VSA_API_BASE=https://your-api-id.execute-api.us-east-1.amazonaws.com/prod \
  --env VSA_ADP_KB_ID=<prod KB id, or omit if no prod KB exists> \
  --env VSA_ADP_REGION=us-east-1 --env VSA_ADP_ACCOUNT_ID=123456789012
```

Backfill the resulting runtime ARN into `STAGE_DEFAULTS.prod.assistantChatRuntimeArn` in `infrastructure/bin/vsa.ts` — this retires the `AVX_RUNTIME_ARN` placeholder-env-var pattern for all subsequent prod deploys.

> **Today's equivalent:** `aws ssm put-parameter --overwrite --type String --name /cvx/prod/assistant-chat-runtime-arn --value <arn>`. See the banner at the top of this section.

**Phase 3 — API stack, customer-facing surface:**

```bash
bash scripts/build-api-vehicle-live-state.sh
bash scripts/build-api-ws-authorizer.sh
cd infrastructure
VSA_STAGE=prod AWS_REGION=us-east-1 npx cdk diff vsa-prod-api -c adpStage=prod   # STOP GATE 3
VSA_STAGE=prod AWS_REGION=us-east-1 npx cdk deploy vsa-prod-api -c adpStage=prod --require-approval never
cd ..
```

No `AVX_RUNTIME_ARN` needed by this phase — the real ARN is now baked into `STAGE_DEFAULTS.prod` from Phase 2B.

**Prod environment reference:**

| Item | Value |
|------|-------|
| Region | `us-east-1` |
| Account | `123456789012` |
| CMS stage | `prod` |
| CMS KMS key | `arn:aws:kms:us-east-1:<account>:key/<cms-kms-key-uuid>` |
| AgentCore execution role | `AmazonBedrockAgentCoreSDKRuntime-us-east-1-prod` (CDK-managed) |
| AgentCore S3 bucket | `vsa-prod-agentcore-codebuild-123456789012-us-east-1` (CDK-managed) |
| AgentCore text runtime | `vsa_supervisor_text_prod` agent in `.bedrock_agentcore.yaml` |
| Foundation Memory | `vsa_prod_cvx_foundation_memory` |
| Athena workgroup | `cvx-prod-analytics` |
| Athena results bucket | `cvx-prod-athena-results-123456789012-us-east-1` |

These now live in SSM under `/cvx/prod/` — see § "Per-Stage Parameters" above. Env-var overrides (`CMS_KMS_KEY_ARN`, `VSA_MEMORY_KMS_KEY_ARN`, `EXTRA_USER_POOL_IDS`, `AVX_RUNTIME_ARN`) still take precedence if set. **`cmsKmsKeyArn` remains the exception**: it was never baked for prod, so it cannot be recovered from git history and must be supplied once when seeding (`CMS_KMS_KEY_ARN=<arn> bash scripts/seed-stage-parameters.sh --stage prod --apply`). The guard that used to catch its absence lived in `vsa-core-stack.ts` and was **deleted** — under SSM the value is a CloudFormation token, which is never empty, so an emptiness check there could not fire. Absence is now caught by CloudFormation failing the stack operation on the missing parameter.

> The placeholdered KMS key above is a real key UUID, deliberately not published. Note it appears **only here** — it is not in `STAGE_DEFAULTS`, so no other file's scrub covers it. Placeholders rather than literals because **this file is meant to ship to a public mirror unchanged.**

**Post-deploy verification:**

```bash
# Preflight probe — confirm the browser CORS preflight succeeds
# (allow ~30-60s after deploy for edge-optimized API Gateway's CloudFront layer to propagate — see Troubleshooting)
curl -sS -D - -o /dev/null -X OPTIONS \
  -H 'Origin: https://<your-cms-frontend-domain>' \
  -H 'Access-Control-Request-Method: POST' \
  https://your-api-id.execute-api.us-east-1.amazonaws.com/prod/assistant/chat | grep -E '^HTTP/'
# Expect: HTTP/2 204

# Authenticated smoke — needs a CMS-prod-pool (<cms-prod-ui-pool-id>) IdToken
VSA_API_URL=https://your-api-id.execute-api.us-east-1.amazonaws.com/prod AWS_REGION=us-east-1 \
  python3 scripts/smoke-test-assistant-chat.py --tenant meridian-fleet --region us-east-1
```

Retain the `cdk diff` outputs from each STOP-GATE phase alongside the deploy commands you actually ran; that record — plus the CFN stack event history — is the audit trail for the cutover.



Run when code under `agents/` or `corpora/` changes. **This is the deploy needed for most agent-only bug fixes.**

**Prerequisite (one-time per machine):** the `agentcore` CLI ships in a separate package, not in `requirements.txt`:

```bash
pip install bedrock-agentcore-starter-toolkit
```

**First deploy** (when `.bedrock_agentcore.yaml` doesn't exist yet, OR after `agentcore destroy`):

```bash
agentcore configure -e agents/supervisor/agentcore_app.py -r us-east-1 --disable-memory
python3 scripts/attach-agentcore-permissions.py    # idempotent; attaches Bedrock model perms to the execution role
agentcore deploy                                   # ~3–5 min on first deploy
```

**Subsequent deploys** (most cases, including this codebase today since `.bedrock_agentcore.yaml` already exists):

```bash
agentcore deploy                                       # default agent (vsa_supervisor_bidi)
agentcore deploy --agent vsa_supervisor                # text-mode agent only
agentcore deploy --agent vsa_supervisor_bidi_staging   # us-west-2 staging voice runtime
agentcore deploy --agent vsa_supervisor_text_prod      # us-east-1 prod text runtime (NEW — cutover 2026-07-17)
```

`agentcore deploy` packages the source tree (per `source_path` in `.bedrock_agentcore.yaml`), uploads to S3, builds a container via CodeBuild, and updates the runtime. Output prints the `agent_arn` and `agent_id`.

**Note on full vs agent-only deploys:** `make deploy` (prod) and `make deploy-staging` now both wrap `agentcore deploy` automatically — see § "Component 1b". Run `agentcore deploy` directly only when you want to push agent-code changes without re-running CDK (the most common case for fast iteration on `agents/`).

**Note:** observability requires CloudWatch Transaction Search enabled in the account — one-time setup per AWS account before the first deploy.

### Component 3: Seeds (run once after fresh CDK deploy)

```bash
python3 scripts/seed-meridian-fleet.py            # tenant config in DynamoDB
python3 scripts/seed-meridian-fleet-user.py       # demo user in Cognito
python3 scripts/seed-personas.py            # persona-specific demo users
python3 scripts/seed-service-network.py     # service center fixtures
python3 scripts/wire-connect-to-tenant.py   # Amazon Connect → tenant routing
```

These seed test data and demo users. Re-run if the underlying tables get destroyed.

### 3. Knowledge base — Acquire + Discover KB corpora (Bedrock Knowledge Base YULHRAUMNU)

Spec `2026-08-04-cvx-oem-discover-order-meridian` adds two new Bedrock KB source categories: `vehicle_manufacturing` (12 docs, reused from prior KB deployments) and `vehicle_shopping_guide` (10 docs, new). Both categories ground the Discover-phase shopping guidance and the Build-phase manufacturing-timeline explanations. This section documents the ingestion, audit, and default threading for staging.

**Prerequisites:**
- ADP staging Bedrock KB `YULHRAUMNU` exists in us-east-1 (created by `2026-06-16-adp-vehicle-knowledge-base` spec).
- Both corpora are present on disk: `corpora/vehicle_manufacturing/` and `corpora/vehicle_shopping_guide/` (shipped by this spec's Group 3).
- `aws cli` credentials configured for cross-account access to the ADP KB.

**Ingestion procedure — two-job sequence (add-then-reflect-deletions gate):**

The two-job sequence protects against zero-content windows during the transition from old corpus names to new ones. `vehicle_shopping_guide` is net-new (no replacement concern), but `vehicle_manufacturing` is a rename of an already-ingested `auto_manufacturing` corpus. Both jobs upload the same S3 prefix structure; the second job reflects deletions after audit-passing the new corpora.

**Job A — add new corpora (upload to S3):**

```bash
# Upload vehicle_manufacturing and vehicle_shopping_guide to staging lake
LAKE_BUCKET="adp-staging-foundation-lake-$(aws sts get-caller-identity --query Account --output text)-us-east-1"

# Clean target prefixes (safe: audit must pass before Job B reflects deletions)
aws s3 rm "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/vehicle-manufacturing/" --recursive --region us-east-1
aws s3 rm "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/vehicle-shopping-guide/" --recursive --region us-east-1

# Upload the new corpora with metadata sidecars
aws s3 cp corpora/vehicle_manufacturing/ \
  "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/vehicle-manufacturing/" \
  --recursive --region us-east-1 --exclude "*" --include "*.md" --include "*.metadata.json"
aws s3 cp corpora/vehicle_shopping_guide/ \
  "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/vehicle-shopping-guide/" \
  --recursive --region us-east-1 --exclude "*" --include "*.md" --include "*.metadata.json"

# Trigger ingestion Job A via the ADP data source (operator-run step)
JOB_A_ID=$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H \
  --region us-east-1 \
  --query 'ingestionJob.ingestionJobId' --output text)

# Poll for completion (~2-5 min). NOTE: the `bedrock-agent` service has
# NO `wait` subcommand — poll `get-ingestion-job` instead.
while :; do
  S=$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id YULHRAUMNU --data-source-id QZL8HLXQ1H \
    --ingestion-job-id "$JOB_A_ID" --region us-east-1 \
    --query 'ingestionJob.status' --output text)
  echo "status: $S"
  { [ "$S" = "COMPLETE" ] || [ "$S" = "FAILED" ]; } && break
  sleep 15
done

# Verify completion
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H \
  --ingestion-job-id "$JOB_A_ID" \
  --region us-east-1 \
  --query 'ingestionJob.{status,numberOfDocumentsScanned,numberOfNewDocumentsIndexed,numberOfDocumentsFailed}'
```

Expected: `status: COMPLETE`, `numberOfNewDocumentsIndexed: 22` (10 shopping-guide + 12 manufacturing), `numberOfDocumentsFailed: 0`.

**Post-ingestion audit (≥90 second AOSS lag):**

After Job A completes, wait ≥90 seconds for the Amazon OpenSearch Serverless index to refresh, then run the audit set:

```bash
# Wait for AOSS index refresh
sleep 95

# Run audit set against the new corpora (both filtered and unfiltered modes)
VSA_ADP_KB_ID=YULHRAUMNU VSA_ADP_REGION=us-east-1 \
  python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json --stage staging

# With category filter enabled (production mode)
VSA_KB_SOURCE_CATEGORY_FILTER=true VSA_ADP_KB_ID=YULHRAUMNU VSA_ADP_REGION=us-east-1 \
  python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json --stage staging --category-filter
```

**Both runs must exit 0 (all 41 prompts pass ≥0.55 threshold).** If any prompts fail unfiltered, the content needs fixing (vocabulary mismatch, typos, or insufficient coverage). Rewrite the offending document(s) to share the query's vocabulary, then re-upload and re-run Job A (the same job ID can be re-triggered; it is idempotent).

**Job B — retire old corpora (reflect deletions):**

After audit-passing with Job A, run Job B to delete the three superseded S3 prefixes and reflect the deletion in the KB:

```bash
# Delete the retired corpora from S3 (only after Job A audit PASSES)
aws s3 rm "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/auto-manufacturing/" \
  --recursive --region us-east-1
aws s3 rm "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/bike-manufacturing/" \
  --recursive --region us-east-1
aws s3 rm "s3://${LAKE_BUCKET}/knowledge/vehicle_knowledge_base/sources/bike-shopping-guide/" \
  --recursive --region us-east-1

# Trigger ingestion Job B (reflects the S3 deletions in the KB)
JOB_B_ID=$(aws bedrock-agent start-ingestion-job \
  --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H \
  --region us-east-1 \
  --query 'ingestionJob.ingestionJobId' --output text)

# Poll for completion (~2-5 min). NOTE: the `bedrock-agent` service has
# NO `wait` subcommand — poll `get-ingestion-job` instead.
while :; do
  S=$(aws bedrock-agent get-ingestion-job \
    --knowledge-base-id YULHRAUMNU --data-source-id QZL8HLXQ1H \
    --ingestion-job-id "$JOB_B_ID" --region us-east-1 \
    --query 'ingestionJob.status' --output text)
  echo "status: $S"
  { [ "$S" = "COMPLETE" ] || [ "$S" = "FAILED" ]; } && break
  sleep 15
done

# Verify completion
aws bedrock-agent get-ingestion-job \
  --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H \
  --ingestion-job-id "$JOB_B_ID" \
  --region us-east-1 \
  --query 'ingestionJob.{status,numberOfDocumentsDeleted,numberOfDocumentsFailed}'
```

Expected: `status: COMPLETE`, `numberOfDocumentsDeleted: 34` (10 + 12 + 12 from three deleted prefixes), `numberOfDocumentsFailed: 0`.

**Post-Job-B audit:**

Wait another ≥90 seconds and re-run the audit set. Now all 41 prompts should pass **unfiltered** (the duplicate old-corpus entries are gone):

```bash
# Unfiltered mode should now pass 41/41 (was 36/41 before Job B due to duplicate ranking)
VSA_ADP_KB_ID=YULHRAUMNU VSA_ADP_REGION=us-east-1 \
  python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json --stage staging
```

**Environment-variable threading — critical distinction:**

The code defaults in `agents/supervisor/config.py` are already flipped to the
generic categories (Group 3 Task 3.4), and the `Makefile` now threads both
values explicitly to each staging runtime. **Neither reaches the deployed
runtime until an operator re-runs `make deploy-staging`** — an already-running
AgentCore runtime keeps the environment it was launched with, so a code-default
change is inert until redeploy.

- **Code defaults in `config.py`**: `acquire_kb_source_category="vehicle_manufacturing"`, `discover_kb_source_category="vehicle_shopping_guide"` (both the dataclass default and the `from_env()` fallback)
- **Makefile threading**: `VSA_ACQUIRE_KB_SOURCE_CATEGORY=vehicle_manufacturing`, `VSA_DISCOVER_KB_SOURCE_CATEGORY=vehicle_shopping_guide` — threaded to BOTH the bidi and text runtimes. The Acquire variable was previously threaded by nothing at all and fell through to the code default; that gap was closed in Group 3 Task 3.4.
- **Live staging runtime**: still resolving whatever it was last deployed with. Until the redeploy, `manufacturing_explain` on the deployed runtime points at the retired category, whose documents were removed from the knowledge base — so it will retrieve nothing. This is the one operator action Group 3 could not perform for you.

Once `make deploy-staging` is run, the code defaults and the threaded env vars agree.

**Flipping to the new categories at deploy time:**

When ready to activate the new KB categories on the live staging runtimes:

```bash
# Deploy with the new KB category env vars (both text and bidi runtimes)
VSA_ACQUIRE_KB_SOURCE_CATEGORY=vehicle_manufacturing \
VSA_DISCOVER_KB_SOURCE_CATEGORY=vehicle_shopping_guide \
make deploy-staging
```

The Makefile threads these vars to both `vsa_supervisor_text_staging` (if present) and `vsa_supervisor_bidi_staging` runtimes via `agentcore deploy --env` arguments. Verify post-deploy:

```bash
# Confirm the env vars reached both runtimes
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <runtime-id> \
  --region us-west-2 --query 'environmentVariables.{ACQUIRE:VSA_ACQUIRE_KB_SOURCE_CATEGORY,DISCOVER:VSA_DISCOVER_KB_SOURCE_CATEGORY}'
```

Expected output: `"ACQUIRE": "vehicle_manufacturing"`, `"DISCOVER": "vehicle_shopping_guide"`.

### Optional: Local Knowledge Base

If ADP's Bedrock KB isn't available (or for offline dev):

```bash
python3 scripts/bootstrap-local-kb.py
```

The agent's `lookup_knowledge` tool falls back to local `corpora/` markdown files when `BEDROCK_KB_ID` is unset.

## Expected Outcome

**After CDK deploy:**
- All 4+ CloudFormation stacks in `CREATE_COMPLETE` or `UPDATE_COMPLETE` in `us-east-1`.
- `aws cloudformation describe-stacks --query 'Stacks[?starts_with(StackName,\`vsa-\`)].StackStatus'` returns `CREATE_COMPLETE` or `UPDATE_COMPLETE` for each.
- API Gateway URL printed in stack outputs (capture into `.env` as `VSA_API_BASE`).

**After staging deploy** (us-west-2):
- `vsa-staging-core`, `vsa-staging-api`, `vsa-staging-triage-classifier` and `vsa-staging-rsa` in `CREATE_COMPLETE` or `UPDATE_COMPLETE`. (`make deploy-staging` is `--all`, so the other `vsa-staging-*` stacks deploy too; these four are the ones the CI smoke test checks.)
- The `deploy-staging` GitHub Actions job runs a stack-status smoke test that exits non-zero if any of those four stacks ends in any other state. <!-- verify: grep -n "for stack in" .github/workflows/evals.yml -->
- AgentCore runtime `vsa_supervisor_bidi_staging` deployed to `us-west-2`; `agent_arn` populated in `.bedrock_agentcore.yaml`.
- The paired Connected Mobility client's iOS `Staging.xcconfig` has `VSA_AGENTCORE_RUNTIME_ARN` auto-filled with the deployed runtime ARN. Other six placeholders (REST/WS URLs, Cognito IDs) still require manual back-fill from the AVX CFN stack outputs after each deploy.

**After `agentcore deploy`:**
- Agent state prints `READY`. Verify with:
  ```bash
  aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> --region us-east-1 \
    --query 'status' --output text
  ```
  Should return `READY`.

**After seeds:**
- Cognito user pool has `demo@meridian-fleet.example` and the persona users.
- DynamoDB `vsa-tenants` table has the `meridian-fleet` tenant config.

## Smoke Tests

Run these post-deploy. They MUST exit zero.

```bash
# Text-mode agent (no voice)
python3 scripts/smoke-test-agentcore.py

# Voice agent (single turn)
python3 scripts/smoke-test-voice.py

# Voice agent (multi-turn)
python3 scripts/smoke-test-voice-multiturn.py

# Cross-account KB Retrieve (staging only, requires VSA_ADP_KB_ID deployment)
python3 scripts/smoke-test-kb-retrieve.py --stage staging

# REST API endpoint
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$VSA_API_BASE/vehicles/VEH-0025/service-history" | jq -e .

# Web chat route (staging only, requires CMS pool to be trusted in vsa-api)
# This smoke test uses the same cached IdToken pattern as smoke-test-agentcore.py
VSA_API_URL=https://<vsa-rest-api-id>.execute-api.us-west-2.amazonaws.com/staging \
python3 scripts/smoke-test-assistant-chat.py
```

For agent-only changes, `smoke-test-agentcore.py` plus the relevant voice test is sufficient.

If any smoke test fails, investigate before declaring the deploy successful — a green CFN stack update is a necessary but not sufficient signal.

### Per-persona Tier-3 smoke (audio, staging-only)

The `evals/runner/tier3_e2e.py` per-persona smoke drives **real spoken-audio
WAVs** through the SigV4 WebSocket path for all six personas (Nova Sonic v2
generates only on audio, not text). It is the automated coverage for
carry-forward **7.B.4**: `service_advisor` + `technician` must each ground on
the live ADP KB and cite ≥1 `s3://` source; the four non-KB personas pass a
liveness check (the agent invokes ≥1 tool on the audio turn).

```bash
# 1. Generate the Polly WAV fixtures (one-time / idempotent; ~negligible cost)
python3 -m evals.scripts.generate_kb_prompt_audio

# 2. Run the smoke (staging-only — see double-gate below)
export AWS_REGION=us-west-2
export RUNTIME_ID=<vsa_supervisor_bidi_staging runtime id>   # e.g. vsa_supervisor_bidi_staging-cF6Xnf7PcX
export VSA_TENANT_ID=meridian-fleet
export VSA_USER_TOKEN=<Cognito id-token>   # or rely on the cached-token mint
pytest evals/runner/tier3_e2e.py -k persona_smoke --staging -v
```

- **Double-gate (intentional):** the smoke runs ONLY when BOTH `--staging` is
  passed AND `RUNTIME_ID` is set. Without either, all six tests **skip** — the
  default unit suite and PR CI never open a live connection.
- **JWT:** `VSA_USER_TOKEN` (a Cognito id-token) is preferred; otherwise the
  harness mints one via the dev `USER_PASSWORD_AUTH` flow from `.env`
  (`VSA_DEMO_USERNAME`/`VSA_DEMO_PASSWORD`). The cached token at
  `~/.vsa/tokens.json` is returned without an expiry check — set
  `VSA_USER_TOKEN` (or clear the cache) if you hit a 401.
- **7.B.4 closure:** `service_advisor` + `technician` use knowledge-phrased
  prompts (TSB / warranty lookups) — fault/DTC-phrased prompts route to
  auto-triage, not the KB. The expected persona → route pairings are
  captured in the Tier 3 harness at `evals/runner/tier3_e2e.py` and the
  case fixtures under `evals/cases/`.

## Tear-Down

```bash
# CDK stacks (prod)
cd infrastructure && cdk destroy --all --force
cd ..

# CDK stacks (staging)
cd infrastructure && VSA_STAGE=staging AWS_REGION=us-west-2 npx cdk destroy --all --force
cd ..

# AgentCore runtime (deletes runtime + ECR repo + S3 bucket) — destroys the DEFAULT agent only
agentcore destroy

# To destroy a SPECIFIC non-default runtime instead (e.g. the prod text runtime
# shipped by the 2026-07-16-cvx-prod-cutover spec, without touching the default
# vsa_supervisor_bidi runtime):
agentcore destroy -a vsa_supervisor_text_prod
```

CDK destroy takes ~5 min. `agentcore destroy` is near-instant. There is intentionally no `make destroy-staging` target — destructive operations require an explicit env-var invocation.

**Manual cleanup after `agentcore destroy`** — the starter toolkit does NOT remove the inline IAM policy attached by `scripts/attach-agentcore-permissions.py`. If tearing down permanently:

```bash
aws iam delete-role-policy \
  --role-name <execution-role-name> \
  --policy-name VsaAgentCoreSupervisorBedrockAccess
```

Reversible via the deploy commands above (seeds need re-running on a fresh deploy).

## Troubleshooting

### `agentcore deploy` fails with "execution role not found"

`.bedrock_agentcore.yaml`'s `execution_role` ARN points at a role that doesn't exist or isn't assumable from this account. Check the role exists:
```bash
aws iam get-role --role-name <role-name> --region us-east-1
```
If missing, run `agentcore init` (one-time) to provision a new role.

### CDK stacks fail to deploy with KMS / DynamoDB errors

CMS isn't deployed (or `CMS_STAGE` / `CMS_KMS_KEY_ARN` in `.env` is wrong). Verify CMS first:
```bash
aws cloudformation describe-stacks --stack-name cms-prod-storage --region us-east-1
```

### `lookup_knowledge` returns `"Knowledge base not configured."`

Two failure modes:
1. `BEDROCK_KB_ID` env unset AND ADP's KB not deployed → bootstrap a local KB: `python3 scripts/bootstrap-local-kb.py`.
2. `_CORPORA_DIR` path resolution wrong → see issue `2026-05-26-dtc-kb-not-configured`.

### Voice agent disconnects mid-turn

Empty tool result + Nova silence → see related iOS issue `2026-05-26-voice-silence-watchdog-empty-tool-result` in CMS repo. Backend cause is usually upstream (KB misconfig).

### `agentcore` CLI reports stale `source_path`

`.bedrock_agentcore.yaml`'s `source_path` and `entrypoint` fields can drift if the repo was renamed or moved (`agentcore configure` records absolute paths at the time it ran). Symptom: `agentcore deploy` uploads the zip successfully, then fails with `Agent endpoint update failed: The specified entrypoint could not be found or accessed in your artifact.`

Fix: edit `.bedrock_agentcore.yaml` and change the absolute paths to relative paths matching `.bedrock_agentcore.yaml.example`:

```yaml
entrypoint: agents/supervisor/bidi_app.py    # not /Users/.../agents/supervisor/bidi_app.py
source_path: .                                # not /Users/...
```

Both `vsa_supervisor` and `vsa_supervisor_bidi` blocks need fixing if both are present. Then re-run `agentcore deploy`. The file is gitignored, so this fix is local-only.

### KB tools silently fall back to local corpora

Symptom: agent tools like `knowledge()`, `parts_lookup()`, or `diy_repair_advisor()` return results from the embedded `corpora/` instead of citing the live Bedrock KB.

**Root cause**: `VSA_ADP_KB_ID` is not set in the deployed AgentCore runtime environment.

**Fix**: Set the env var and redeploy:

```bash
VSA_ADP_KB_ID=YULHRAUMNU make deploy-staging
```

Verify post-deploy: `aws bedrock-agentcore-control get-agent-runtime ... --query 'environmentVariables.VSA_ADP_KB_ID' --output text` should return the KB ID (not empty).

**Additional note on source-category filtering**: KB tools can filter on `source_category` metadata for persona-scoped retrieval, gated by `VSA_KB_SOURCE_CATEGORY_FILTER` (code default `false`). As of 2026-06-22 the ADP KB `YULHRAUMNU` carries per-document `source_category` metadata (ADP spec `2026-06-22-adp-vkb-content-fill`, ingestion `DAD31WBERT`), so `make deploy-staging` now threads `--env VSA_KB_SOURCE_CATEGORY_FILTER=true` to enable persona scoping. Verified: filtered Retrieve on each persona's category set returns correctly-scoped live KB docs. Set the var to `false` (or unset) only when pointing at a KB that lacks the metadata tag. See issue `2026-06-18-cvx-kb-source-category-metadata-mismatch` for history.

### `cdk deploy vsa-prod-core` (or `-diff`) proposes reverting IAM grants to staging buckets

Symptom: `cdk diff vsa-prod-core` (or `-triage-classifier` / `-api`, which depend on core) shows an unexpected diff swapping `adp-prod-foundation-lake-...` / `cvx-prod-athena-results-...` grants to their `adp-staging-...` / `cvx-staging-...` equivalents, even though the live role is currently correct.

**Root cause**: `infrastructure/lib/vsa-core-stack.ts` reads an undocumented CDK context flag, independent of `VSA_STAGE`:

```typescript
const adpStage: string = this.node.tryGetContext('adpStage') ?? 'staging';
```

This governs two cross-project (ADP) S3 IAM grants only. Omitting `-c adpStage=prod` from a prod `cdk deploy`/`cdk diff` invocation silently resolves to the `'staging'` fallback.

**Fix**: always pass `-c adpStage=prod` on every `cdk deploy`/`cdk diff` against `vsa-prod-core` and any stack that depends on it (`vsa-prod-triage-classifier`, `vsa-prod-api`). The commands in this doc's § "Component 1c: CDK + AgentCore prod" already include it. A follow-on tracked improvement is to derive the value from `stage` instead of carrying an independent context flag; that is deliberately deferred to keep the cutover change-set minimal.

### First post-deploy OPTIONS/GET probe against a new prod route returns 403 `MissingAuthenticationTokenException`, but the deploy succeeded

Symptom: immediately (within ~30-60s) after a `cdk deploy vsa-prod-api` that adds a new route, the first `curl` probe against that route returns `403` with `x-amzn-errortype: MissingAuthenticationTokenException` and `cloudfront`/`via` response headers — looks like the route doesn't exist, even though CFN reports `UPDATE_COMPLETE` and `aws apigateway get-method` confirms the method is correctly configured.

**Root cause**: this is normal edge-cache propagation lag on API Gateway's own AWS-managed CloudFront layer — every edge-optimized `execute-api.*.amazonaws.com` endpoint is served through CloudFront transparently, and a route change needs a short window to propagate to edge caches. The `cloudfront`/`via` headers are NOT evidence of a rogue/misconfigured custom CloudFront distribution (confirmed via `aws cloudfront list-distributions` during the 2026-07-16 prod cutover — no distribution in this account targets the AVX API Gateway as an origin).

**Fix**: wait ~30-60s and retry. Do not conclude the deploy failed based on a probe run immediately after `cdk deploy` completes.

Symptom: `make deploy-staging` exits before synth with `VSA_ADP_KB_ID='<value>' is not a valid Bedrock KB id (expected 8-16 upper-alphanumeric chars, e.g. YULHRAUMNU)`.

**Root cause**: `VSA_ADP_KB_ID` is set but malformed (lowercase, wrong length, or a wildcard like `*`). The pre-flight format guard rejects it.

**Why it exists**: a wildcard or malformed value would thread into `-c adpKbId=...` and re-emit the cross-account IAM grant as `arn:...:knowledge-base/*`, silently undoing the fail-closed KB scoping from spec `2026-06-18-cvx-adp-kb-wireup`. Set a real KB ID (e.g. `VSA_ADP_KB_ID=YULHRAUMNU`).

### `scripts/attach-agentcore-permissions.py` no longer needed

The script `scripts/attach-agentcore-permissions.py` is retired from the `make deploy-staging` workflow as of spec `2026-06-18-cvx-adp-kb-wireup` (issue `2026-06-16-attach-agentcore-permissions-malformed-policy`). CDK-managed AgentCore execution roles get all required Bedrock/Memory/DynamoDB/KMS permissions via IAM policy statements in `infrastructure/lib/vsa-core-stack.ts`. Do NOT run the script manually for CDK-managed deployments — it is a no-op.

## Reference

- Full README quickstart: `README.md` → "Quick Start"
- AgentCore runtime architecture: `docs/ARCHITECTURE.md`
- Eval pipeline (Tier 1/2/3): `docs/EVAL.md`, `make eval-all`
- Tenant config schema: `schemas/vsa-tenant-config.schema.json`
## AVX-on-foundation v1 deploy

AVX-on-foundation v1 re-binds AVX personas to ADP-owned data products (vehicle identity, service records, charging history, OTA campaigns, customer history, knowledge base) via two grounding mechanisms: **AgentCore Memory** for tabular data (metadata-filtered LTM writes at session-init) and **Bedrock KB cross-account Retrieve** for reference data (TSBs, DTCs, service policies, parts catalog). This section covers the operator steps to deploy v1 on top of the existing AVX staging infrastructure.

### Pre-flight

Before beginning the deploy, verify these prerequisites:

1. **ADP Lake Formation v4 cross-account settings** — on the ADP side (account 123456789012, region us-east-1):
   ```bash
   aws lakeformation get-data-lake-settings --region us-east-1 \
     --query 'DataLakeSettings.Parameters.CROSS_ACCOUNT_VERSION' --output text
   ```
   Must return `"4"`. If absent or `"3"`, ADP operator must run:
   ```bash
   aws lakeformation put-data-lake-settings --region us-east-1 \
     --data-lake-settings '{
       "DataLakeAdmins": [{"Principal": {"DataLakePrincipalIdentifier": "arn:aws:iam::123456789012:root"}}],
       "Parameters": {"CROSS_ACCOUNT_VERSION": {"StringValue": "4"}, "ALLOW_EXTERNAL_DATA_FILTERING": {"BooleanValue": true}}
     }'
   ```

2. **ADP Knowledge Base deployment status** — the ADP vehicle knowledge base **is deployed** (`YULHRAUMNU`, us-east-1, same account) and is the live grounding source for KB-backed tools. Verified 2026-08-05: a 41-prompt retrieval audit passes at the 0.55 threshold (`python3 scripts/smoke-test-kb-retrieve.py --audit-set evals/kb_audit/meridian_audit_set.json --stage staging`). Pass `VSA_ADP_KB_ID=YULHRAUMNU` to `make deploy-staging`, or the runtime gets an empty `adp_kb_id` and KB-grounded tools silently fall back to local corpora.

3. **Cognito custom attribute** — for 3 expansion personas (Vehicle Owner, Service Advisor, Technician), add a `persona_id` custom attribute to the AVX Cognito user pool **before** running the seed script:
   ```bash
   POOL_ID=$(aws cloudformation describe-stacks --stack-name vsa-staging-core \
     --region us-west-2 --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' --output text)
   aws cognito-idp add-custom-attributes --user-pool-id "$POOL_ID" \
     --custom-attributes Name=persona_id,AttributeDataType=String,Mutable=true,Required=false
   ```
   If the attribute already exists (e.g., from a prior run), this is a no-op and safe to re-run.

4. **Pre-deploy CloudFormation validation** — after `cdk synth`, validate the templates before deploying (per Group 7.A fix-cycle findings):
   ```bash
   cd infrastructure && npx cdk synth >/dev/null 2>&1
   for stack in cdk.out/vsa-staging-*.template.json; do
     aws cloudformation validate-template --template-body "file://$stack" --region us-west-2 >/dev/null || exit 1
   done
   ```
   All templates must pass CFN service-side validation. If any fail, inspect the error — common issue: see "Troubleshooting § CFN early-validation rejects on AgentCore Memory" below.

### Deploy commands (5 steps)

**Step 1: AgentCore Memory + KMS (us-west-2, ~3 min)**

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_ACCOUNT=123456789012 \
  CDK_DEFAULT_REGION=us-west-2 npx cdk deploy vsa-staging-foundation \
  --require-approval never
cd ..
```

This creates:
- `vsa-staging-cvx-foundation-memory` (AgentCore Memory, metadata-tagged LTM for tabular data)
- AVX-managed KMS CMK + alias for Memory encryption
- Capture the Memory ID from CFN stack output: `CvxFoundationMemoryId`. You'll need this for Step 5.

**Step 2: Athena workgroup + results bucket (us-east-1, ~30s)**

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 \
  npx cdk deploy vsa-staging-analytics --require-approval never
cd ..
```

This creates:
- `cvx-staging-analytics` workgroup in Athena (for cross-region Glue catalog queries)
- `cvx-staging-athena-results-123456789012-us-east-1` S3 bucket (results bucket, region-suffixed per cross-region namespace discipline)

**Step 3: IAM role update for cross-account access (us-west-2, ~30s)**

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy vsa-staging-core \
  --context adpAccountId=123456789012 \
  --context adpStage=staging \
  --require-approval never
cd ..
```

This updates the AgentCore execution role (`vsa-staging-agentcore-execution-us-west-2`) with new IAM policy statements:
- `AdpGlueAthenaRead` — permission to query ADP's Glue catalog via Athena
- `AdpLakeFormationGetDataAccess` — Lake Formation credential brokering for data access
- `AdpLakeKmsDecrypt` — decrypt ADP lake KMS-encrypted Parquet files
- `AdpBedrockKbRetrieveCrossAccount` — cross-account Bedrock KB Retrieve (currently broad; will tighten to specific KB ARN in Step 5 when KB deploys)
- `CvxAgentCoreMemoryOps` — create/read Memory records in us-west-2
- `CvxAthenaResultsS3` — read/write Athena results bucket in us-east-1

**Step 4 (DEFERRED per Q9): Tighten KB Retrieve policy to specific KB ARN**

When ADP ships the Bedrock KB (`guidance-for-vehicle-knowledge-base` CDK stack), the KB ARN will be known. At that time, re-run:

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk deploy vsa-staging-core \
  --context adpAccountId=123456789012 \
  --context adpStage=staging \
  --context adpBedrockKbArn=arn:aws:bedrock:us-east-1:123456789012:knowledge-base/<kb-id> \
  --require-approval never
```

This tightens `AdpBedrockKbRetrieveCrossAccount` from `bedrock:knowledge-base/*` to the specific KB ARN.

**Step 5: AgentCore runtime deploy with v1 configuration (us-west-2, ~5 min)**

```bash
MEMORY_ID=$(aws cloudformation describe-stacks --stack-name vsa-staging-foundation \
  --region us-west-2 --query 'Stacks[0].Outputs[?OutputKey==`CvxFoundationMemoryId`].OutputValue' --output text)

agentcore deploy -a vsa_supervisor_bidi_staging \
  --auto-update-on-conflict \
  --env CMS_STAGE=staging \
  ... # see the warning below — do NOT hand-roll this list
```

> ⚠️ **Do not copy a hand-written `--env` list from this document.** Corrected
> 2026-09-21 after an audit found the list that used to appear here was wrong in
> three independent ways.
>
> `--env` is the **only** source of runtime environment. `.bedrock_agentcore.yaml`
> persists none of it, and `agentcore deploy` does **not** merge with the running
> configuration — whatever you pass becomes the entire environment. Any variable
> you omit is silently deleted from the runtime.
>
> The list previously printed here carried 13 variables against a runtime that
> needs 17. It omitted `VSA_API_BASE` (its absence makes the triage tool build an
> unscheme'd `/triage` URL and raise `requests.MissingSchema`), `VSA_CMS_REGION`,
> `VSA_KB_SOURCE_CATEGORY_FILTER`, and all four `VSA_ACQUIRE_*` / both
> `VSA_DISCOVER_*` values. It named five variables the runtime no longer uses
> (`VSA_ADP_STAGE`, `VSA_ATHENA_WORKGROUP`, `VSA_ATHENA_RESULTS_BUCKET`,
> `VSA_PERSONA_LTM_TTL_MIN`, `VSA_MEMORY_REGION`) — leftovers from the
> persona-loader era. And it set `VSA_ADP_KB_ID=` **empty**, which would have
> broken KB retrieval outright.
>
> **`make deploy-staging` is the source of truth** (`Makefile:221-239`). It
> resolves `VSA_FOUNDATION_MEMORY_ID` and `VSA_API_BASE` from CFN outputs and
> hard-fails if either is unresolvable, precisely so a deploy cannot silently
> ship a runtime that raises on every persona session. Prefer it.
>
> If you must deploy the runtime alone without the CDK step, derive the env set
> from what is actually running rather than from any document:
>
> ```bash
> RID=vsa_supervisor_bidi_staging-cF6Xnf7PcX
> aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RID" \
>   --region us-west-2 --query environmentVariables --output json > /tmp/env.json
> ENV_ARGS=$(python3 -c "
> import json, shlex
> e = json.load(open('/tmp/env.json'))
> print(' '.join(f'--env {shlex.quote(f\"{k}={v}\")}' for k, v in sorted(e.items())))")
> agentcore deploy -a vsa_supervisor_bidi_staging --auto-update-on-conflict $ENV_ARGS
> ```
>
> That reproduces the current environment exactly, so the only thing the rebuild
> changes is the code and its pinned dependencies. Verify afterwards that the
> count is unchanged (17 at the time of writing) — a drop means variables were
> dropped.

This builds and deploys the voice-mode AgentCore runtime with the new persona-loader module, cross-account Athena/KB configuration, and AgentCore Memory metadata-tagging.

### ADP-side handoff

Once v1 is deployed on the AVX side, coordinate with the ADP operator:

1. **Apply Lake Formation v4 settings** (one-time per AWS account) — if not already done at pre-flight step 1:
   ```bash
   # Run on ADP side (us-east-1)
   aws lakeformation put-data-lake-settings --region us-east-1 \
     --data-lake-settings '{
       "DataLakeAdmins": [{"Principal": {"DataLakePrincipalIdentifier": "arn:aws:iam::123456789012:root"}}],
       "Parameters": {"CROSS_ACCOUNT_VERSION": {"StringValue": "4"}, "ALLOW_EXTERNAL_DATA_FILTERING": {"BooleanValue": true}}
     }'
   ```

2. **Share AVX execution role ARN with ADP operator** — the role ARN is printed in vsa-staging-core CFN outputs:
   ```bash
   aws cloudformation describe-stacks --stack-name vsa-staging-core --region us-west-2 \
     --query 'Stacks[0].Outputs[?OutputKey==`AgentCoreExecutionRoleArn`].OutputValue' --output text
   ```
   Give this ARN to the ADP operator; they'll use it when ADP CDK deploys with the KB resource policy (Step 4 deferred item).

3. **(When ADP KB deploys)** ADP operator runs from the `automotive-data-platform-on-aws/guidance-for-vehicle-knowledge-base` directory:
   ```bash
   # Run on ADP side
   .venv/bin/cdk deploy \
     -c stage=staging \
     -c cvxKbPrincipals=arn:aws:iam::123456789012:role/AmazonBedrockAgentCoreSDKRuntime-us-west-2-vsa-staging
   ```
   This deploys the KB and attaches a resource policy allowing the AVX role to call `bedrock-agent-runtime:Retrieve`.

### Smoke tests

Run these in sequence after all 5 deploy steps complete. All must exit zero.

```bash
# 1. Cross-account Athena read (tests Glue catalog + Lake Formation access)
python3 scripts/smoke-test-cross-account.py --stage staging
# Expected: 200ms–500ms, returns sample row from adp_staging_vehicle_identity
```

```bash
# 2. Persona-loader end-to-end (tests Memory writes + Athena + persona snapshot)
# Use a real VIN and customer ID from the seeded test data
python3 scripts/load_persona_smoke.py --stage staging \
  --persona vehicle_owner \
  --vin 1FA00000000000009 \
  --customer-id CUST-00000000
# Expected: 5–7 seconds, loads 3–6 products depending on ADP data availability
# Products loaded: vehicle_identity, customer_360, ota_campaigns (typical for vehicle_owner)
# Products not loaded: charging_sessions, service_records, vehicle_telemetry_aggregated (zero-row results or PySpark-gated)
```

```bash
# 3. (Deferred) Cross-account KB Retrieve smoke — skip until ADP KB deploys
# When the KB is live, test: scripts/smoke-test-kb-retrieve.py --stage staging
```

```bash
# 4. Per-persona Tier 3 smoke — skip until full bidi WebSocket setup
# When ready: scripts/evals/tier3/persona-smoke-all.py --stage staging
```

### Tear-down

If you need to destroy the v1 infrastructure and start fresh:

```bash
# Order: core IAM/role dependencies first, analytics second, foundation last
cd infrastructure

# 1. Core stacks (core → api → triage-classifier)
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk destroy vsa-staging-core vsa-staging-api vsa-staging-triage-classifier \
  --force

# 2. Analytics stacks (us-east-1)
VSA_STAGE=staging AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 \
  npx cdk destroy vsa-staging-analytics --force

# 3. Foundation stacks (Memory + KMS, us-west-2)
VSA_STAGE=staging AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx cdk destroy vsa-staging-foundation --force

cd ..
```

**Manual cleanup after destroy:**

- AgentCore Memory and KMS have `RemovalPolicy.RETAIN` — they survive stack deletion. Verify the role has no references to them, then delete via the console or AWS CLI.
- Athena results bucket also has RETAIN. Once confirmed no results are needed, delete via the console.

### Troubleshooting

**CFN early-validation rejects on AgentCore Memory**

Symptom: `aws cloudformation validate-template` or `cdk deploy` fails with property-shape errors like:
- "is not less or equal to 365" — `eventExpiryDuration` is in **days** (not seconds); max 365 days.
- "does not match pattern" — Memory name pattern `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` (alphanumeric + underscore only; no hyphens).

**Fix**: see `infrastructure/lib/agentcore-memory-cr.ts`. The CDK L1 construct correctly synthesizes to CFN, but ensure:
- `eventExpiryDuration` is in days (e.g., `90` for 90 days).
- Memory name uses underscore-only: `vsa_staging_cvx_foundation_memory` (not `vsa-staging-cvx-foundation-memory`).
- Indexed keys in `addPropertyOverride` use PascalCase (`Key`/`Type`, not camelCase `keyName`/`keyType`).

**persona-loader returns 0 products**

Symptom: `load_persona_smoke.py` runs successfully but `products loaded: 0 of N`.

**Fix**: common causes:
- Athena `GetQueryResults` with `MaxResults=1` returns header only → use `MaxResults=2` (header + first data row).
- `BatchCreateMemoryRecords` is on the `bedrock-agentcore` **data-plane** client, not control-plane (`bedrock-agentcore-control`).
- Timestamp parameter expects `datetime.datetime`, not int epoch milliseconds.
- Empty-string metadata values (e.g., `customer_id=""`) are rejected; omit the key instead if value is empty.

**`attach-agentcore-permissions.py` fails with MalformedPolicyDocument**

Symptom: running `python3 scripts/attach-agentcore-permissions.py` fails with `botocore.errorfactory.MalformedPolicyDocument`.

**Fix**: skip the script for CDK-managed roles. When the AgentCore execution role is created and managed by CDK (as in this deployment), the required Bedrock/Memory/DDB/KMS permissions are already attached via IAM policy statements in `vsa-core-stack.ts`. The script is redundant for this deployment.

**AgentCore runtime fails to start with "unknown environment variable"**

Symptom: AgentCore deploy succeeds but runtime status is `FAILED` or logs show `KeyError: 'VSA_FOUNDATION_MEMORY_ID'`.

**Fix**: verify all 10 `--env` flags were passed to `agentcore deploy` in Step 5. Missing flags cause the runtime container to fail at startup. Redeploy with all flags present.

**Memory `list_memory_records` returns 0 results post-write**

Symptom: `load_persona_smoke.py` writes to Memory successfully (no boto3 error) but subsequent Memory reads return empty.

**Cause**: likely indexing delay (Memory records are eventually consistent; ~5–10s after write) or namespace-filter mismatch in the read query.

**Workaround for v1**: the persona-loader smoke test is not the runtime — if `CreateEvent` / `BatchCreateMemoryRecords` succeed (no exception), data is in Memory and will be readable by the agent within a few seconds. Manual testing can verify by running `load_persona_smoke.py` once, waiting 10s, then calling `bedrock-agentcore:RetrieveMemoryRecords` directly.

**Full investigation**: tracked as v1.1 follow-on `AVX LTM indexing delay` (P3).

**PySpark-gated products (`vehicle_telemetry_aggregated`, `energy_usage`) always return "not available"**

**Expected behavior in v1**: these two ADP products live in databases not yet in the LF cross-account share (they ship with the `PySpark via Glue` spec, currently IDLE Architect). The persona-loader checks if the source Glue tables exist; if absent, returns "not yet available" and skips the Athena query.

**When PySpark via Glue ships**: the Glue tables populate in `adp_staging_vehicle_telemetry_aggregated` and `adp_staging_energy_usage` databases. No AVX code change needed — the persona-loader's existence check flips automatically and Memory begins receiving events. ADP operator must separately extend the LF wildcard share to include those two databases (tracked as ADP follow-on row `PySpark cross-account ramp-on`).

**AgentCore runtime loses custom headers**

Symptom: when adding new `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` headers in `bidi_app.py` (e.g., `X-Amzn-Bedrock-AgentCore-Runtime-Custom-PersonaId`), the headers are not received by the runtime.

**Fix**: also update `.bedrock_agentcore.yaml::request_header_configuration.requestHeaderAllowlist` to include the new header name, then redeploy with `agentcore deploy`.

### v1.1 follow-on items

These are known gaps or refinements tracked for the v1.1 iteration:

- **AVX LTM streaming refresh** — v1 uses lazy 15-minute TTL refresh; v1.1 wires streaming notifications via Kinesis Data Streams (GA 2026-03) for real-time LTM updates on ADP data changes.
- **AVX LTM indexing delay** — unclear if Memory `list_memory_records` consistency delay is expected or a bug; v1.1 investigation to quantify and potentially add automatic retry-with-backoff in smoke tests.
- **Persona-loader latency** — v1.0 measured 6–7s at session-init (3s PRD budget); likely sequential Athena queries not parallelizing. v1.1 optimization via true `asyncio` parallelization across products.
- **CloudTrail data-events on KB Retrieve** — v1 has management-event coverage; v1.1 adds data-event logging for per-call Retrieve audit (requires `AWS::CloudTrail::EventSelector` for `bedrock:Retrieve` operations).
- **ADP Knowledge Base ARN in AVX config** — when ADP KB deploys, AVX should persist the KB ARN as a CFN custom resource / environment variable to enable e2e KB Retrieve validation in Tier 3 evals; v1.1 deployment automation.
- **attach-agentcore-permissions.py retire or fix** — the script is redundant for CDK-managed roles but might be useful for legacy/manual AgentCore setups; a v1.1 decision to deprecate or fix the policy-document bug is a follow-on.

## Acquire / Discover deploy (spec `2026-08-04-cvx-oem-discover-order-meridian`)

The Buy surface in the iOS app (DiscoverFlow -> ConfiguratorFlow) needs four
things live: the `vsa-<stage>-acquire` stack, a seeded catalog **for the
tenant the demo user belongs to**, the AgentCore text runtime carrying the
Discover tools and env vars, and the `vsa-<stage>-api` stack carrying the
`/assistant/chat` Lambda.

### 1. Stack

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 \
  npx cdk deploy vsa-staging-acquire --exclusively --require-approval never -c adpKbId=YULHRAUMNU
```

Creates three DynamoDB tables — `vsa-staging-acquire-{catalog,orders,leads}` —
all PAY_PER_REQUEST, PITR on, `RemovalPolicy.RETAIN`.

> Use `--exclusively` and set `AWS_REGION` when deploying a single stack.
> Without them CDK attempts a cross-region VPC lookup and fails with
> `Could not find any VPCs matching {... "region":"us-east-1" ...}`. The
> `_deploy-cdk` Makefile target sets `AWS_REGION` for this reason.

### 2. Seed the catalog — PER TENANT

**This is the step most likely to be missed.** The Discover tools scope every
read by `ctx.tenant_id`, which comes from the caller's `custom:tenantId`
Cognito claim — never from the request body. A catalog seeded for one tenant
is invisible to a user in another, and the failure is silent: the agent simply
reports it found no models.

Check which tenant a demo user actually belongs to before assuming:

```bash
aws cognito-idp admin-get-user --user-pool-id <pool> --username <email> \
  --region us-west-2 --query "UserAttributes[?Name=='custom:tenantId'].Value" --output text
```

Then seed that tenant. The committed catalog is `genericmoto-demo`; override
`tenantId` for any other tenant:

```bash
source .venv/bin/activate
python3 scripts/seed-acquire-catalog.py \
  --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json \
  --table vsa-staging-acquire-catalog --region us-west-2
```

Verify per tenant (expect 74 items):

```bash
aws dynamodb query --table-name vsa-staging-acquire-catalog --region us-west-2 \
  --key-condition-expression "tenantId = :t" \
  --expression-attribute-values '{":t":{"S":"<tenant>"}}' --select COUNT --query Count --output text
```

> `seed-acquire-catalog.py` has **no `--tenant` flag** — the `tenantId` is
> baked into the catalog JSON. To seed another tenant, copy the JSON, rewrite
> `tenantId` on the envelope and on every item, and seed the copy. Internal
> tenant catalogs must be produced outside the repo (`/tmp`) and never
> committed, per the tenant canary rules.

### 3. Knowledge base

Two corpora ship in `corpora/`: `vehicle_shopping_guide` (Discover grounding for vehicle shopping) and
`vehicle_manufacturing` (post-order narration for the four-wheeler Meridian tenant). Both carry
`source_category` metadata sidecars.

```bash
B=adp-staging-foundation-lake-$(aws sts get-caller-identity --query Account --output text)-us-east-1
P=knowledge/vehicle_knowledge_base/sources
aws s3 sync corpora/vehicle_shopping_guide/  "s3://$B/$P/vehicle-shopping-guide/"  --region us-east-1 \
  --exclude "*" --include "*.md" --include "*.md.metadata.json"
aws s3 sync corpora/vehicle_manufacturing/  "s3://$B/$P/vehicle-manufacturing/"  --region us-east-1 \
  --exclude "*" --include "*.md" --include "*.md.metadata.json"

aws bedrock-agent start-ingestion-job --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H --region us-east-1 --description "acquire/discover corpora"
```

Expect `numberOfDocumentsFailed: 0`. **The AOSS index lags ingestion
COMPLETE by roughly 50-90s** — a retrieve immediately after will return
nothing. Wait, then:

```bash
VSA_ADP_KB_ID=YULHRAUMNU VSA_ADP_REGION=us-east-1 \
  python3 scripts/smoke-test-kb-retrieve.py \
  --query "how do I choose between a commuter and an adventure bike" --num-results 3
```

Top-hit score should clear 0.55.

#### Meridian four-wheeler corpus (Zone 2)

Four additional categories ship for the Zone 2 Meridian tenant — 24 documents
plus 24 sidecars — alongside the two-wheeler corpora above, which they must NOT
disturb:

| Local dir | S3 prefix | `source_category` |
|---|---|---|
| `corpora/vehicle_manufacturing/` | `vehicle-manufacturing/` | `vehicle_manufacturing` |
| `corpora/meridian_lineup_narrative/` | `meridian-lineup-narrative/` | `meridian_lineup_narrative` |
| `corpora/meridian_owner_manual/` | `meridian-owner-manual/` | `meridian_owner_manual` |
| `corpora/meridian_ive_package/` | `meridian-ive-package/` | `meridian_ive_package` |

Note the convention mismatch, which is deliberate and load-bearing: **S3 prefixes
are hyphenated, `source_category` values are underscored.** Retrieval filters on
the sidecar value, not the prefix.

```bash
B=adp-staging-foundation-lake-$(aws sts get-caller-identity --query Account --output text)-us-east-1
P=knowledge/vehicle_knowledge_base/sources
for pair in "vehicle_manufacturing:vehicle-manufacturing" \
            "meridian_lineup_narrative:meridian-lineup-narrative" \
            "meridian_owner_manual:meridian-owner-manual" \
            "meridian_ive_package:meridian-ive-package"; do
  aws s3 cp "corpora/${pair%%:*}/" "s3://$B/$P/${pair##*:}/" --recursive --region us-east-1
done

aws bedrock-agent start-ingestion-job --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H --region us-east-1 --description "Meridian four-wheeler corpus"
```

Use `aws s3 cp`, never `sync --delete` — the same prefix holds 158 two-wheeler
objects across ten other categories that must survive untouched. A correct run
reports `numberOfNewDocumentsIndexed: 24`, `numberOfDocumentsDeleted: 0`, and
`numberOfDocumentsFailed: 0`.

#### Per-tenant `source_category` configuration

These are the values the runtime should carry. They now match the generic
defaults in `config.py` (`vehicle_manufacturing` and `vehicle_shopping_guide`,
flipped in Group 3 Task 3.4), so threading them explicitly documents intent at
the deploy seam rather than relying on a code default:

```
VSA_ACQUIRE_KB_SOURCE_CATEGORY=vehicle_manufacturing
VSA_DISCOVER_KB_SOURCE_CATEGORY=meridian_lineup_narrative
```

**This is not optional polish.** During development, the two-wheeler and four-wheeler manufacturing corpora shared all ten stage names and
their filenames (`bike-manufacturing/04-sub-assembly.md` vs
`vehicle-manufacturing/04-sub-assembly.md`), creating the potential for an unfiltered manufacturing query to return the wrong vehicle class with high confidence — measured 2026-08-03 at
bike 0.6449 versus auto 0.6031 for the same prompt. The filter exists to prevent this silent failure: the
agent answering fluently about a motorcycle when asked about a vehicle.

#### Re-running the audit gate

```bash
# The gate — mirrors the configured-tenant path. Must exit 0.
VSA_ADP_KB_ID=YULHRAUMNU python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json --category-filter

# Diagnostic — quantifies what an unconfigured tenant would see.
VSA_ADP_KB_ID=YULHRAUMNU python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json
```

Baseline results and near-miss margins:
`evals/kb_audit/baselines/2026-08-03-meridian-audit.md`. Filtered was 31/31;
unfiltered was 30/31 by design. Any prompt scoring below 0.55 is fixed by
rewriting the document to share the query's vocabulary — never by lowering the
threshold.

### 4. Runtimes and API

`make deploy-staging` covers both, and threads the `VSA_ACQUIRE_*` /
`VSA_DISCOVER_*` env vars to each runtime. For a targeted runtime redeploy
(e.g. a prompt change), run `agentcore deploy -a vsa_supervisor_text_staging
--auto-update-on-conflict` after the CDK stacks are current.

> **Which deploy carries which change** — this costs time if you get it
> wrong. Agent tools, personas and prompts live in the **AgentCore runtime**
> (`agentcore deploy`). The `/assistant/chat` Lambda lives in the
> **`vsa-staging-api` CDK stack** (`cdk deploy`). A change to
> `lambdas/api-assistant-chat/handler.py` will NOT go live from an
> `agentcore deploy`, and vice versa.

### 5. Smoke tests

```bash
# transport + fleet_driver regression — run this after ANY runtime/prompt change
python3 scripts/smoke-test-assistant-chat.py --api-url "$API" --tenant meridian-fleet

# Discover path, as the demo user, with the consumer persona override
curl -s -X POST "$API/assistant/chat" -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"I need an electric bike for a 15km city commute.",
       "sessionId":"smoke_0000000000000000000000000000","personaId":"consumer"}'
```

`sessionId` must be **>= 33 characters** (AgentCore minimum) and the JSON key
must be `sessionId` — the handler rejects anything else with
`400 sessionId is required`.

### 6. Kiosk device setup (Zone 2)

**⚠️ NOTE on tenant scoping:** `custom:tenantId` in the Cognito user is **the catalog tenant** (e.g., `meridian` for the Meridian configurator), **NOT the pool's label** (e.g., `meridian-fleet` for the Meridian Fleet tenant pool). These are different and the mismatch is a live trap. Check before creating users.

Each physical kiosk (iPad/tablet) authenticates as its own per-device principal in the `kiosk` Cognito group. Credentials are provisioned once at device setup and never entered by a visitor.

**Create the kiosk user:**

Resolve identifiers from the **stable CloudFormation exports**, not from output keys —
CDK appends a generated suffix to output keys (`PoolMeridian FleetUserPoolIdF56A6711`),
which changes if the construct tree is refactored. The export names below are
declared explicitly in `tenant-pool.ts` and are stable. All of these live on the
`vsa-<stage>-api` stack; there is no separate per-tenant pool stack.

```bash
STAGE=staging
REGION=us-west-2

exp() {  # resolve a CloudFormation export by name
  aws cloudformation list-exports --region "$REGION" \
    --query "Exports[?Name=='$1'].Value" --output text
}

POOL_ID=$(exp "vsa-${STAGE}-meridian-fleet-pool-id")
GROUP=$(exp "vsa-${STAGE}-meridian-fleet-kiosk-group-name")     # "kiosk"

# custom:tenantId MUST be the catalog tenant, not the pool label
aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" \
  --message-action SUPPRESS \
  --user-attributes \
    Name=custom:tenantId,Value=meridian \
    Name=email,Value=kiosk-tabletN@example.com \
    Name=email_verified,Value=true \
  --region "$REGION"

# Add the user to the kiosk group. This is what drives persona clamping to
# `consumer` and the 10-tool allow-list — without it the device is NOT a kiosk.
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --group-name "$GROUP" --region "$REGION"

# Set a permanent password. Generate it, hand it to the device, do not commit it.
aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --password "$(openssl rand -base64 24)" \
  --permanent --region "$REGION"
```

Verify the two claims that actually matter before trusting the device:

```bash
aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "kiosk-tabletN" \
  --region "$REGION" --query "UserAttributes[?Name=='custom:tenantId'].Value" --output text
# expect: meridian

aws cognito-idp admin-list-groups-for-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region "$REGION" --query 'Groups[].GroupName' --output text
# expect: kiosk
```

**Provision the device:**

```bash
CLIENT_ID=$(exp "vsa-${STAGE}-meridian-fleet-kiosk-client-id")
```

Use **this** client id on the tablet, not the default app client. It is
**SRP-only** by design (`userSrp: true`, `userPassword: false`,
`adminUserPassword: false`), so `USER_PASSWORD_AUTH` and `admin-initiate-auth`
will NOT work against it — a provisioning tool needs an SRP implementation, and
the `VSACompanion` app performs SRP on the tablet. Token validity is 5 min
access/id and 480 min (8 h) refresh, so a tablet re-authenticates roughly once
per show day.

# Store the credentials securely on the device (encrypted storage, Keychain/Secure Enclave on iOS)
```

**Enable Guided Access on the iPad** (single-app mode, visitor cannot exit):

- Connect iPad to Xcode, mount to developer account
- Settings → Accessibility → Guided Access → Enable
- Triple-tap Home button at booth open (or configure auto-start) to activate
- Specify which app (VSACompanion) is allowed

**Post-setup verification:**

```bash
# Confirm the kiosk user is in the group
aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region us-west-2 \
  --query "UserAttributes[?Name=='custom:tenantId'].Value" --output text
# Expect: meridian

# Confirm the group assignment
aws cognito-idp admin-list-groups-for-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region us-west-2 \
  --query "Groups[*].GroupName" --output text
# Expect: kiosk
```

### 7. Kiosk device teardown (Zone 2)

Revoking one tablet is the whole point of the named-principal model — an
unauthenticated identity pool would give you no such handle. After the show, or
immediately if a tablet is lost or misbehaving:

```bash
STAGE=staging
REGION=us-west-2
POOL_ID=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='vsa-${STAGE}-meridian-fleet-pool-id'].Value" --output text)

# Preferred: disable. Reversible, and blocks new token issuance immediately.
aws cognito-idp admin-disable-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region "$REGION"

# Also revoke outstanding refresh tokens, otherwise the tablet keeps working
# until its current 8-hour refresh token expires.
aws cognito-idp admin-user-global-sign-out --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region "$REGION"

# Irreversible: remove the principal entirely.
aws cognito-idp admin-delete-user --user-pool-id "$POOL_ID" \
  --username "kiosk-tabletN" --region "$REGION"
```

Disabling alone stops *new* sign-ins; the already-issued access token remains
valid for up to 5 minutes and the refresh token for up to 8 hours, which is why
the global sign-out matters when you are revoking a device mid-show rather than
tearing down after it.

### 8. Guardrail post-deploy verification (Zone 2)

Run this after **every** deploy that touches `vsa-<stage>-core` or the text
runtime, before allowing public traffic. A runtime missing
`VSA_KIOSK_GUARDRAIL_ID` enforces nothing and still returns HTTP 200 — the
failure is silent, which is exactly how the guardrail once reached
staging unwired.

```bash
STAGE=staging
REGION=us-west-2
RUNTIME_ID=vsa_supervisor_text_${STAGE}-yxYZomEgFz   # from `agentcore status`

# 1. The guardrail id must be present on the runtime environment.
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
  --query 'environmentVariables.VSA_KIOSK_GUARDRAIL_ID' --output text
# expect a guardrail id, e.g. 26xr9g9yntdx — NOT "None" and not empty

# 2. It must match what the stack actually deployed.
aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='vsa-${STAGE}-kiosk-guardrail-id'].Value" --output text

# 3. AGENTCORE_STAGE must be set, or the enforcer's fail-closed branch
#    never engages and a missing guardrail id disables silently.
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
  --query 'environmentVariables.AGENTCORE_STAGE' --output text
# expect: staging (or prod)
```

Then confirm it is actually *enforcing*, with a kiosk token (see § Kiosk device
setup) and a deliberately off-topic prompt:

```bash
curl -s -X POST "$API_BASE/assistant/chat" \
  -H "Authorization: Bearer $KIOSK_ID_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"Write me a poem about the ocean.","sessionId":"guardrail-verify-000000000001"}'
```

Expected — the canned refusal plus the `blocked` flag, and nothing else:

```json
{"result": "I'm not able to help with that request at this kiosk. Please ask me
about Meridian vehicles, features, or the order process.",
 "sessionId": "guardrail-verify-000000000001", "blocked": true}
```

Corroborate in the runtime log, which names the guardrail that fired:

```
KioskGuardrailEnforcer: BLOCKED source=INPUT guardrail_id=26xr9g9yntdx
  action='GUARDRAIL_INTERVENED' topics=['off-topic-non-vehicle']
```

Always pair this with a **positive control** — ask a legitimate question
("What does the Windrose cost?") and confirm a real answer comes back. A
guardrail that blocks everything looks identical to a working one if you only
ever test the negative case.

**If the id is missing:** do not hand-patch the runtime. Fix the deploy path so
it cannot recur — `make deploy-staging` and `make deploy` both resolve the id
from the stack export and fail fast if it is unresolvable — then redeploy.

### Rollback

- Prompt or tool change: `git revert` and re-run `agentcore deploy`.
- Buy tab: revert the `MainTabView.swift` Buy-tab block; the other five tabs
  are untouched by it.
- The three Acquire tables are `RETAIN`, so a stack destroy leaves data
  behind and a redeploy will fail on existing table names.
- Kiosk teardown: disable or delete the user per § Kiosk device teardown above to revoke one device.

## Tier 2 Platform Health Agent (Staging)

Scheduled autonomous agent, four components. Three infer vehicle health insights from ADP's cross-account data lake (battery degradation trends, tire wear patterns, brake service intervals); the fourth, `diagnostics`, reads CMS's active fault codes and is not ADP-backed — see "The diagnostics component" below. Runs three daily jobs (battery / tires / brakes) at 02:00 / 02:20 / 02:40 UTC plus `diagnostics` every 5 minutes, writes deterministic health findings to DynamoDB, and emits CloudWatch alarms on anomalies. No Bedrock invocation — the agent runs the deterministic judgment path only; `tokens_used=0` in every run record is correct, not a meter bug.

### VIN selection and cursor advance

**Per-component VIN window** (spec `2026-08-10-cvx-tier2-vin-selection-and-alerting`):
- **Tires**: priority pool 7,630 VINs (`needs_replacement=true`), batch size 156 per run → 49 nights for full pool pass
- **Brakes**: priority pool 69,376 VINs (`outcome IN ('parts_pending','follow_up_required')`), batch size 2,000 per run → 35 nights for full pool pass
- **Battery**: rotation over 100,000 VINs (no priority pool), batch size 375 per run → 267 nights for full pool pass

**Fleet coverage is NOT achieved.** These horizons represent the time to process each vendor's priority pool or rotation once, not periodic re-evaluation. Event-driven selection (follow-on 2) is the path to dynamic prioritisation.

**Cursor item**: DynamoDB stores cursor state (`vsa-staging-tier2-cursor`) keyed by component, tracking `last_vin`, `updated_at`, and `pool_exhausted_count`. **This item must not be deleted casually**—deletion resets the window to the beginning of the pool, causing duplicate artifacts and wasting cost. It is marked `RemovalPolicy.RETAIN` at stack destroy.

### Eight CloudWatch alarms (per-component)

Six new alarms added (spec `2026-08-10`): three `no-vins-selected` (one per component) fire when `VinsSelected == 0` for 1 day straight (sensitive, no legitimate zero); three `artifacts-stale` (one per component) fire when `ComputedAt` has not updated in 3 days (catches long-term failures). Two existing alarms `(1)` no-vins-selected and `(2)` artifacts-stale (stage-only, now deprecated) are retained for metric pattern coverage. All eight carry non-empty `AlarmActions` targeting an operator SNS topic.

**Operator SNS topic**: `vsa-staging-tier2-operator-alerts` in us-west-2. Operator must subscribe manually (no auto-subscription):
```bash
aws sns subscribe --topic-arn arn:aws:sns:us-west-2:<account>:vsa-staging-tier2-operator-alerts \
  --protocol email --notification-endpoint <operator-email>
```

**Outstanding**: operator email address is not seeded automatically. Subscription requires manual confirmation by the operator via email link.

**Optional email parameter**: SSM parameter `/cvx/staging/tier2-alarm-email` (optional) can be seeded to accept an operator email at deploy time (not yet wired to auto-subscribe; seeding requires explicit operator action via AWS Console or `aws sns subscribe`).

Spec: `2026-08-03-cvx-tier2-platform-health-agent` (old) and `2026-08-10-cvx-tier2-vin-selection-and-alerting` (new, adds selection window and alarms). Two coordinated stacks in two regions:
- **`vsa-staging-tier2`** (us-west-2) — Lambda function, DynamoDB table (health findings + cursor), IAM roles, EventBridge schedules, 8 CloudWatch alarms (per-component)
- **`vsa-staging-analytics`** (us-east-1) — Lake Formation grants for the Tier 2 role, pinned to the Glue catalog region

**Deploy order matters.** Tier 2 role must exist before analytics applies Lake Formation grants to it. Deploying analytics first fails with `Invalid principal, arn:...role/vsa-staging-tier2-scheduler-us-west-2`. The CDK dependency (`analyticsStack.addDependency(tier2Stack)`) encodes the order correctly for `cdk deploy --all`; for a first deploy or Tier 2-only updates, deploy targeted stacks with explicit ordering (see commands below).

### Prereqs

- AWS credentials for the AVX account (`<account>`), us-west-2 + us-east-1
- CDKToolkit bootstrapped in both us-west-2 and us-east-1 (both `UPDATE_COMPLETE`)
- Node.js 22
- Docker running: the Tier 2 and broker Lambda assets are bundled in `public.ecr.aws/sam/build-python3.12` (x86_64). Check with a bounded `docker ps` first; a wedged Docker Desktop makes `cdk diff`/`deploy` hang at "Bundling asset ..." with no error
- **Pre-deploy data check (mandatory):**
  ```bash
  python3 scripts/verify-tier2-adp-reads.py --stage staging
  ```
  Must exit 0. Confirms live ADP data is present: `energy_usage` 10,000,000 rows / 100,000 VINs; `tire_health` 6,000,800 rows / 25,100 VINs; `service_records` 499,713 brake rows / 475,657 VINs / 69,861 open jobs.

### Deploy Commands

**Option 1: Full deploy** (both stacks in correct order)

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 npx cdk deploy vsa-staging-tier2 --require-approval never
VSA_STAGE=staging AWS_REGION=us-west-2 npx cdk deploy vsa-staging-analytics --require-approval never
cd ..
```

Note `AWS_REGION=us-west-2` for BOTH commands. Analytics is pinned to us-east-1 in code; `AWS_REGION` is what makes the role name it grants to resolve to `...-us-west-2`.

**Option 2: Tier 2 only** (if updating agent code or Lambda without touching analytics / LF grants)

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 npx cdk deploy vsa-staging-tier2 --require-approval never
cd ..
```

**Why NOT use `make deploy-staging`:** It runs `cdk deploy --all` plus both AgentCore runtime deploys, requires `VSA_ADP_KB_ID` (not needed for Tier 2), and takes 5–10 minutes. Use it for full-stack refreshes; use the targeted commands above for Tier 2-specific changes.

### Expected Outcome

**After tier2 deploy:**
- 14 resources created in `vsa-staging-tier2` (Lambda, DynamoDB, roles, alarms, schedules)
- CFN outputs: `HealthFindingsTableName=vsa-staging-health-findings`, `Tier2HealthAgentFnArn=arn:aws:lambda:us-west-2:<account>:function:vsa-staging-tier2-health-agent-us-west-2`, two alarm ARNs
- **Four** **ENABLED** EventBridge schedules: `vsa-staging-tier2-daily-{battery,tires,brakes}-us-west-2` at 02:00 / 02:20 / 02:40 UTC, plus `vsa-staging-tier2-diagnostics-us-west-2` at `rate(5 minutes)`, each passing `{"stage":"staging","component":"<c>"}`

**After analytics deploy:**
- 6 Lake Formation principal permissions created (DESCRIBE on database + SELECT/DESCRIBE on table wildcard per product, per ADP's strict-mode lake architecture)
- `vsa-staging-analytics` stack in `UPDATE_COMPLETE`

### The diagnostics component

Added 2026-09-25 (spec `2026-09-25-avx-own-vehicle-findings`). It differs from the other three in
every operational respect, so treat it separately when reading alarms or planning a deploy.

| | battery / tires / brakes | diagnostics |
|---|---|---|
| Source | ADP data lake, via Athena | CMS `cms-{stage}-storage-dtc-history`, plus the vehicles registry for the VIN |
| Schedule | daily, 02:00 / 02:20 / 02:40 UTC | `rate(5 minutes)` |
| Schedule name | `vsa-{stage}-tier2-daily-{component}-{region}` | `vsa-{stage}-tier2-diagnostics-{region}` |
| Finding expiry | the component's own TTL | **30 minutes** after the last write (`DIAGNOSTICS_TTL_SECONDS`, `agents/health/cms_dtc_source.py`) |
| Retries | per `componentSchedules` | none — the next run is 5 minutes away and redoes the work |
| Per-component alarms | `NoVinsSelected`, `ArtifactsStale` | **neither**, deliberately |

Two consequences worth knowing before you read a dashboard:

- **A fleet with no active fault codes is healthy, not broken.** The `NoVinsSelected` and
  `ArtifactsStale` alarms assume a daily ADP batch, so they are not wired for `diagnostics` — an
  empty run is the expected steady state. Failures still page: `run_diagnostics` raises after any
  failed write, which surfaces on `Tier2RunFailureAlarm`.
- **A cleared fault's card disappears on its own.** The runner refreshes an unchanged Finding's
  expiry every run, so a card outlives its fault by at most one TTL — about 30 minutes. Do not
  expect a delete.

The VIN on a diagnostics Finding comes from the **vehicles registry**, not from the DTC row
(`issues/2026-09-28-diagnostics-vin-taken-from-dtc-rows/`). Registry rows are the authority; a DTC
row's `vin` is stale or absent on most staging rows, and taking it wrote Findings under a VIN no
vehicle is registered with, which no owner or driver could read.

### Smoke Test

```bash
python3 scripts/smoke-test-tier2.py --stage staging
```

Exits non-zero on failure. Verifies:
- Lambda invokes successfully for each component (battery / tires / brakes; `diagnostics` is not in the smoke test — it is verified from its own schedule's runs)
- Each produces the expected row count (`rows_read`): battery 2500, tires 6000, brakes 25
- Each accounts for 25 health-finding artifacts as **DynamoDB items** in
  `vsa-staging-health-findings` — one per VIN. On a first run these appear as
  `artifacts_written=25`; on any later run over unchanged ADP data they appear as
  `duplicates_skipped=25` with `artifacts_written=0`, which is idempotency (M6) working
  rather than a failure. The smoke test asserts on the sum, so both are a PASS.
  Athena's own query output goes to S3 (the workgroup's `athena-results/` prefix) and is
  unrelated to these artifacts — do not look in S3 for a missing finding.
- Each artifact carries every M4 field, with non-empty `evidence[]` and `sk` ending in
  `inputs_hash`
- No VIN-shaped token appears in the Lambda's CloudWatch output

**Variants:**
```bash
# Test a single component only
python3 scripts/smoke-test-tier2.py --stage staging --component battery

# Skip actual invocation (useful for dry-run validation)
python3 scripts/smoke-test-tier2.py --stage staging --no-invoke
```

### Tear-Down

```bash
# Destroy tier2 stack (DynamoDB table + log group have RemovalPolicy.RETAIN)
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 npx cdk destroy vsa-staging-tier2 --force
cd ..
```

**Important:** DynamoDB table and CloudWatch log group survive stack deletion (`RemovalPolicy.RETAIN`). If genuinely removing all state, delete them manually afterward via the console or AWS CLI.

**Critical caveat:** If you delete the Tier 2 role while analytics Lake Formation grants still reference it, the grants enter `Invalid principal` state. Destroy order: (1) destroy analytics LF grants first by updating or destroying the analytics stack, OR (2) re-repair the grants via the analytics stack redeploy after tier2 is gone.

### Troubleshooting

**`Invalid principal` on LfDbTier2* permissions**

Lake Formation grant references a role ARN that no longer exists. See Deploy order above — deploy tier2 first, analytics second. If this error occurs after a deploy:

```bash
# Check stack creation sequence from events
aws cloudformation describe-stack-events --stack-name vsa-staging-analytics \
  --region us-east-1 --query 'StackEvents[?ResourceType==`AWS::LakeFormation::PrincipalPermissions`].[Timestamp,ResourceStatus,ResourceStatusReason]' --output table
```

If analytics was created first (before tier2), destroy analytics, then redeploy in the correct order.

**`TABLE_NOT_FOUND: awsdatacatalog.default.<table>`**

Athena SQL missing database qualifier. The `cvx-staging-analytics` workgroup has no default database set, so unqualified table names fail. Each component's SQL must name the database — `adp_staging_<product>.<table>` — and each source module owns its own query. The Lambda entrypoint is `agents/tier2/runner.py:lambda_handler` (the live Lambda's `Handler` is `agents.tier2.runner.lambda_handler`); the SQL itself lives in `agents/health/adp_battery_source.py`, `adp_tire_source.py`, and `adp_brake_source.py`. Structurally-similar defects also hit here: paginated `GetQueryResults` responses that silently truncate at the first page, and `CAST(array<varchar> AS VARCHAR)` on a column that is genuinely an array.

**`Insufficient Lake Formation permission(s)`**

The ADP lake is strict-mode Lake Formation. IAM permissions alone are insufficient; explicit LF grants must exist. Verify all six:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE="arn:aws:iam::${ACCOUNT}:role/vsa-staging-tier2-scheduler-us-west-2"

for P in energy_usage tire_health service_records; do
  DB="adp_staging_${P}"
  DBP=$(aws lakeformation list-permissions --region us-east-1 \
      --principal "{\"DataLakePrincipalIdentifier\":\"${ROLE}\"}" \
      --resource "{\"Database\":{\"CatalogId\":\"${ACCOUNT}\",\"Name\":\"${DB}\"}}" \
      --query "join(',', PrincipalResourcePermissions[].Permissions[])" --output text)
  TBP=$(aws lakeformation list-permissions --region us-east-1 \
      --principal "{\"DataLakePrincipalIdentifier\":\"${ROLE}\"}" \
      --resource "{\"Table\":{\"CatalogId\":\"${ACCOUNT}\",\"DatabaseName\":\"${DB}\",\"TableWildcard\":{}}}" \
      --query "join(',', PrincipalResourcePermissions[].Permissions[])" --output text)
  printf "  %-32s db=[%s]  table=[%s]\n" "$DB" "$DBP" "$TBP"
done
```

Expected, verified against staging 2026-08-05:

```
  adp_staging_energy_usage         db=[DESCRIBE]  table=[DESCRIBE,SELECT]
  adp_staging_tire_health          db=[DESCRIBE]  table=[DESCRIBE,SELECT]
  adp_staging_service_records      db=[DESCRIBE]  table=[DESCRIBE,SELECT]
```

Any empty bracket means that grant is missing — redeploy `vsa-staging-analytics`, and check the
deploy order above, because a grant created before its principal existed fails and rolls back.

Two API details worth knowing before you try a shorter command, because both cost time:
`list-permissions` **requires `--resource` whenever `--principal` is given** (otherwise
`InvalidInputException: Resource is mandatory if Principal is set in the input`), which is why
this loops per resource rather than asking once. And an unfiltered `list-permissions` does **not**
enumerate every principal's grants — run against this account it returns ~53 rows and **none** of
them is the Tier 2 role — so filtering the unfiltered list client-side reports zero grants on a
correctly-granted deployment.

**`rows_read=0` with no error**

Query succeeded but no rows. Common causes: LF grants missing (see above), Glue table doesn't exist, or CMK permissions insufficient. Check:

1. Glue table exists: `aws glue get-table --database-name adp_staging_<product> --name <table> --region us-east-1`
2. LF grants exist: `aws lakeformation list-permissions ...` (command above)
3. CMK decrypt permission exists on the Tier 2 role for the ADP lake CMK

**`AthenaResultTooLargeError`**

Query results exceed memory. Tune Lambda environment variables (on `vsa-staging-tier2` stack):
- `TIER2_MAX_ROWS_PER_QUERY` (default 50000) — raise if query times out waiting for rows
- `TIER2_MAX_VINS_PER_RUN` (default 25) — lower to reduce rows per batch

Redeploy after changing:

```bash
cd infrastructure
VSA_STAGE=staging AWS_REGION=us-west-2 \
  npx cdk deploy vsa-staging-tier2 \
  -c tier2MaxRowsPerQuery=100000 \
  -c tier2MaxVinsPerRun=10 \
  --require-approval never
cd ..
```

### Implementation Notes

The Tier 2 Lambda (`vsa-staging-tier2-health-agent-us-west-2`) runs only the deterministic judgment path:
- `runner._default_agent_fn()` returns `run_health_agent`
- `make_strands_agent_fn` has no callers
- No third-party ML packages bundled
- Result: `tokens_used=0` and `estimated_cost_usd=0.0` in every run record are correct and expected, not indicative of a broken meter or missing Bedrock integration

The Lambda **does** carry Bedrock `InvokeModel` and `InvokeModelWithResponseStream` grants — two statements in `vsa-tier2-stack.ts`, scoped to the Claude Sonnet 4.6 inference profile and foundation model. They are currently **unexercised**, not absent: `runner._default_agent_fn()` returns the deterministic `run_health_agent`, `agent.make_strands_agent_fn` has no callers anywhere in the codebase, and the Lambda asset bundles no third-party packages, so even a wired Strands path would `ImportError` and fall back. Granting an entitlement nothing consumes is worth flagging rather than describing as absent; it is recorded as a follow-on in the spec's `decisions.md` (wire it or remove it, but do not leave live grants beside an unreachable code path).

## Public-Mirror Publish (Maintainers)

This section is for maintainers who publish tagged releases from this internal
repo to the public GitHub mirror
`aws-solutions-library-samples/guidance-for-agentic-vehicle-experience-on-aws`.
End-user deploys do not need any of this.

### Files that govern the publish

| File | Purpose |
|---|---|
| `.publish-exclude` | Gitignore-syntax manifest of paths stripped from the published tree (internal docs, per-developer state, house-style diagram sources, etc.) |
| `.publish-secrets-scan.yml` | Secret-scanner config: `forbidden_patterns` (regex), `forbidden_strings` (literals), and `scan_exclude` (paths skipped from scanning for load-bearing exceptions with written rationale per entry) |
| `scripts/publish-to-github.sh` | Publish driver: validates preconditions, clones-and-strips into a staging tree, runs the scanner, squash-pushes to the public mirror |
| `scripts/lib/secret-scan.py` | Standalone scanner invoked by the publish script (works against any tree via `--root`) |

### Pre-publish dry-run (required before any real push)

```bash
# From the repo root
bash scripts/publish-to-github.sh --tag vX.Y.Z --dry-run
```

Expected clean output:

```
Files after:   311
Scanner:       clean
Staging dir:   /var/folders/.../cvx-publish-vX.Y.Z-...   (preserved for review)
Dry-run complete. No changes pushed.
```

If the scanner reports findings, the exit code is non-zero and the staging
directory is preserved so you can inspect the offending files. Do NOT
proceed to a real push until the dry-run is `clean`. If findings surface,
resolve them by pattern class in a small dedicated fix cycle and re-run the
dry-run until clean; the v0.1.0 milestone was one such cycle
(130 → 0 findings).

### First-time publish checklist (v0.1.0)

1. Working tree clean, on `main`
2. Annotated semver tag: `git tag -a v0.1.0 -m "..."`
3. Tag pushed to origin: `git push origin v0.1.0`
4. Pre-publish dry-run reports `Scanner: clean`
5. Trigger the manual `publish_to_github` job in the GitLab pipeline (or
   run `scripts/publish-to-github.sh --tag v0.1.0` without `--dry-run` if
   the GitLab job is not yet wired up)
6. Post-publish: `git fetch github` and check that the mirror's `main` and
   the new tag point at the freshly-published squash commit; check CodeQL
   Default Setup runs green within 10 minutes; check Dependabot alerts
   auto-close.

The real-mirror push and post-publish verification are explicitly out of
scope of the initial v0.1.0 mechanism spec — the
spec ships the publish MECHANISM ready for use, not the first live push.

### If a future release needs to move the tag

The publish script consumes whatever commit the tag points to. If a
Fix Group cycle needs a re-tag:

```bash
git tag -d vX.Y.Z                                # delete local
git tag -a vX.Y.Z -m "..."                       # re-create at HEAD
git push --force-with-lease origin vX.Y.Z        # push force-moved tag
bash scripts/publish-to-github.sh --tag vX.Y.Z --dry-run
```

Only force-move a tag pre-publish — once the tag has been consumed by a
real publish, treat it as immutable and cut a new patch tag instead.

---

## Zone 2 Kiosk — Edge Throttle + Budget Backstop (decisions.md Q9)

An API Gateway **Usage Plan** (`vsa-<stage>-acquire-usage-plan`) is attached to the REST API
deployment stage. It applies a stage-wide token-bucket throttle to ALL routes, including
the six `/acquire/*` routes and `/assistant/chat`.

### Limits (defaults)

| Metric | Value | Rationale |
|---|---|---|
| Rate limit | **500 req/s** | 2× headroom over 80 visitors × 5 req/s peak burst |
| Burst limit | **200 tokens** | Token bucket capacity; absorbs short spikes |

Throttling real show-floor visitors is a worse outcome than an unexpected bill — these
limits are an **abuse bound**, not a capacity control. Override via `throttleRateLimit` /
`throttleBurstLimit` props on `VsaApiStack` if the show scale changes.

The per-session turn counter in `agentcore_app.py` complements this: it caps one stuck
or adversarial session, but fails **open** on counter-store errors (must not take the
booth down). The edge throttle is the backstop that cannot be bypassed by rotating the
client-supplied `runtimeSessionId`.

### Post-deploy verification

```bash
# Confirm usage plan is attached to the deployed stage
aws apigateway get-usage-plans --region us-west-2 \
  --query "items[?name=='vsa-staging-acquire-usage-plan'].{name:name,throttle:throttle}" \
  --output table
```

Expected: one row with `rateLimit: 500.0`, `burstLimit: 200`.

### Budget alarm (operator step — not automated in CDK)

Create an AWS Budgets cost alert on the show account if a hard spending cap is needed.
This is out of scope for CDK (a CloudFormation-managed budget would fire on every
`cdk deploy` update). Create it manually from the Billing console before the show.

---

## Zone 2 Kiosk — Preflight Gate (REQUIRED before every staging or prod deploy)

**No staging or prod deploy that touches the acquire stack (Zone 2 Groups 2 or 4) may run until `make preflight` exits 0.**

```bash
source .venv/bin/activate
make preflight
```

### Why this gate is mandatory

`session_manager_or_none()` in `agents/supervisor/session.py` gates on the presence of `ctx.jwt`. Under spec D2, kiosk devices carry a valid JWT. Deploying the order backend (Group 2) or its CDK infrastructure (Group 4) without Group 3's kiosk-hardening runtime means kiosk sessions silently attach real AgentCore Memory long-term memory, and one visitor's context can bleed into the next visitor's session on the same device. This is a verified ordering hazard (decisions.md Q10).

The preflight target runs two test modules:

| Test module | What it asserts |
|---|---|
| `agents/supervisor/tests/test_kiosk_gating.py` | Tool allow-list is exactly 10 (not 20+); `session_manager_or_none()` returns `None` for kiosk; `is_kiosk` propagates end-to-end through real `SessionContext` |
| `lambdas/api-assistant-chat/tests/test_persona_map.py` | `kiosk` group resolves to `consumer`; client-supplied persona override is ignored; claimless input does not default to owner-scoped `fleet_driver` |

### Expected output (green)

```
🔒 Zone 2 preflight: verifying kiosk-hardening tests before deploy...
...
38 passed, 2 warnings in 0.9s
✅ Preflight passed — kiosk-hardening tests green. Safe to deploy.
```

### If preflight fails

A non-zero exit means one or more kiosk-hardening tests have regressed. Do NOT proceed with the deploy. Read the pytest output — it will name the failing test. The most likely causes are:

- `is_kiosk` is no longer propagated through the runtime payload → check `lambdas/api-assistant-chat/handler.py` and `agents/supervisor/agentcore_app.py`
- The tool allow-list was widened (a new tool was added without checking kiosk exclusion) → check `agents/supervisor/supervisor.py` `_KIOSK_TOOL_ALLOW_LIST`
- The persona default was changed back to `fleet_driver` → check `persona_map.py` and `agentcore_app.py`

A failing preflight is a deployment blocker, not a warning. Fix the regression, re-run `make preflight`, and only then proceed to `make deploy-staging` or the prod deploy sequence.

---

## Zone 2 Kiosk — Guardrail Post-Deploy Verification

After every staging or prod deploy that touches the AgentCore text runtime or
the CDK acquire stack, **verify that `VSA_KIOSK_GUARDRAIL_ID` is set** in the
runtime environment before allowing any public kiosk traffic.

If the variable is missing, `KioskGuardrailEnforcer.__init__` raises
`ValueError` on the first invocation and the runtime returns a 500 to every
request — this is intentional fail-closed behaviour. A missing variable
on a deployed runtime is a deployment error, not a code error.

### Verification command

```bash
# Replace <runtime-arn> with the deployed text runtime ARN from CDK outputs.
# NOTE: the operation is `get-agent-runtime` on the `bedrock-agentcore-control`
# service. `aws bedrock-agentcore describe-agent-runtime` does not exist —
# `bedrock-agentcore` is the data-plane service and has no such operation.
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <runtime-id> \
  --region us-west-2 \
  --query 'agentRuntimeArtifact.containerConfiguration.containerUri' \
  --output text

# Verify the guardrail ID on the text-runtime agent:
RUNTIME_ID=$(grep -A 5 'vsa_supervisor_text_<stage>' .bedrock_agentcore.yaml \
  | grep 'agent_id:' | awk '{print $NF}')

aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$RUNTIME_ID" \
  --region <region> \
  --query 'EnvironmentVariables.VSA_KIOSK_GUARDRAIL_ID' \
  --output text
```

Expected output: a non-empty Bedrock Guardrail ID (format: `<alphanumeric-string>`).
If the output is `None` or empty, the text runtime was deployed without the guardrail ID. Redeploy:

```bash
agentcore deploy --agent vsa_supervisor_text_<stage> \
  --env VSA_KIOSK_GUARDRAIL_ID=<guardrail-id> \
  --env AGENTCORE_STAGE=<stage>
```

### Why this check exists

The `KioskGuardrailEnforcer` fails closed on staging/prod (`AGENTCORE_STAGE ∈
{staging, prod}`) when `VSA_KIOSK_GUARDRAIL_ID` is unset — it raises at
construction time so the container crash-loops visibly rather than silently
passing all prompt-injection attempts. A CDK stack drift or rushed redeploy
that omits the variable produces an obvious crash loop, not a silent security
regression.

Local development is exempt: when `AGENTCORE_STAGE` is unset (or any value
other than `staging`/`prod`), the enforcer is disabled with a WARNING log.

## Publishing to the Public GitHub Mirror

AVX releases to the public AWS Solutions Library mirror via the scripts vendored under `scripts/` in this repository. This section describes the drift-check, dry-run, and publish flow.

### Before You Publish

1. **Verify the vendored publish-toolkit files are consistent** with the sync
   anchor:
   ```bash
   bash scripts/lib/verify-publish-toolkit-sync.sh .
   # Exit 0 means the vendored files match the anchor (no drift)
   ```

2. **Test a dry-run**:
   ```bash
   bash scripts/publish-to-github.sh --tag v0.1.0 --dry-run
   # Review the file count and scanner output
   ```

### The Publish Flow

1. **Drift check** (step 0) — verifies the vendored publish toolkit hasn't been edited in-place in this repo
2. **Tag validation** — confirms the tag exists and working tree is clean
3. **Staging** — clones at the tag into a temp dir
4. **Strip** — removes files via `.publish-exclude` (including `scripts/lib/.publish-toolkit-sync`)
5. **Scan** — runs the secret scanner + `.publish-secrets-scan.yml` forbidden patterns
6. **Publish** — triggers the GitLab `publish_to_github` manual job (view progress in Pipelines)

The scanner config, exclusion list, and secret patterns are all in-tree — see `.publish-secrets-scan.yml` and `.publish-exclude` at the repo root.

### CI Drift Check Note

AVX does not run a GitLab CI drift-check job; the drift check (step 0 of `publish-to-github.sh`) is the mandatory enforcement and runs locally before publish. AVX cannot publish with drifted toolkit files regardless of CI job status.


## AVX API Stack — Tier 1 AgentCore Grant Ordering (W10, 2026-09-19)

**The `vsa-{stage}-api` stack grants read access to three AVX DynamoDB tables to the
`AmazonBedrockAgentCoreSDKRuntime-{region}-{stage}` IAM role.** This role is auto-provisioned
by `agentcore configure` and lives outside CDK. The grant statements reference the role by name
via `iam.Role.fromRoleName`, which constructs the ARN token at synth time WITHOUT looking up the
role — so `cdk synth` succeeds regardless of whether the role exists.

**HOWEVER**: if the role does NOT exist at deploy time, CloudFormation will fail to attach the
policy at stack update time with an IAM role-not-found error.

### Required deploy ordering for a fresh environment

```
# Step 1: provision the AgentCore execution role
agentcore configure -a vsa_supervisor_text_{stage}
# (or any vsa_supervisor_* configure that creates the role)
# This provisions AmazonBedrockAgentCoreSDKRuntime-{region}-{stage}.

# Step 2: deploy the api stack (Tier 1 grants reference the now-existing role)
cd infrastructure
npx cdk deploy vsa-{stage}-api --require-approval never
```

**Do NOT reverse this order** — deploying `vsa-{stage}-api` before `agentcore configure` runs
will fail with:
```
CloudFormation::Policy: role/AmazonBedrockAgentCoreSDKRuntime-us-west-2-staging does not exist
```

### Subsequent deploys (role already provisioned)

Once the role exists, `cdk deploy vsa-{stage}-api` can run in any order. The role is persistent
and is NOT deleted by `agentcore configure` or `agentcore deploy` on subsequent runs.

### Build AVX Lambda assets before CDK synth

The three AVX Lambdas (findings, actions, register-device) require a pre-build step to bundle
`lambdas/_lib/avx_scope.py` alongside each handler. Run this before `cdk synth` or `make deploy`:

```bash
bash scripts/build-avx-lambda.sh
```

This populates `infrastructure/build/api-avx-*/` directories referenced by `Code.fromAsset()`.
The build script is idempotent and fast (no Docker, no cross-compilation for pure-Python Lambdas).



## RSA Stub Dispatch — Deploy Runbook

Roadside assistance dispatch runs as a **simulator** in this Guidance — the
supervisor recommends a target vendor + ETA and the executor stub writes a
`vsa-{stage}-rsa-dispatch` DynamoDB record, but no real roadside provider is
contacted.

The roadside path spans two stacks, and they deploy together:

- **`vsa-{stage}-rsa`**: dispatch table `vsa-{stage}-rsa-dispatch`, its stream consumer, the
  consumer DLQ, the scheduled lifecycle advancer, the operator SNS topic, and four alarms.
- **`vsa-{stage}-api`**: `POST /escalate`, which writes REQUESTED rows to the dispatch table, and
  `GET /rsa/dispatch/{dispatchId}`, the tenant-scoped status read. The API stack imports the dispatch
  table's exports from the RSA stack.

### Provider mode: fake only

The provider is **simulated**. The consumer and status-read Lambdas run with `VSA_RSA_CLIENT=fake`,
set in CDK; an unset variable also resolves to `fake` (`lambdas/shared/rsa_client.py`). The fake provider returns a `SIM-` reference and an ETA, and the advancer moves the row
DISPATCHED → EN_ROUTE → ARRIVED. No truck is sent. `real` is not implemented: `RealRsaProvider` raises
on every call, and `is_covered()` raises outside fake mode, so a flipped variable fails closed rather
than dispatching. Do not change it; there is no real provider in this guidance.

### Prereqs

- AWS credentials for the target account (`aws sts get-caller-identity`).
- `CVX_RSA_ALARM_EMAIL` set explicitly: an email address, or the literal `none` to deploy the
  alarms with **no subscriber** (nobody is paged). `make` targets refuse to run without it. A bare
  `cdk deploy` with it unset also creates no subscription and only prints a synth warning, so always
  set it.
- Optional timing overrides for the RSA stack, validated at synth (the synth fails on a bad value):
  <!-- verify: grep -n "_EN_ROUTE_AFTER_SECONDS\|_ARRIVED_AFTER_SECONDS\|_STALE_AFTER_SECONDS\|stale < 600" infrastructure/lib/vsa-rsa-stack.ts lambdas/rsa-dispatch-consumer/lifecycle.py -->
  - `CVX_RSA_SIM_EN_ROUTE_AFTER_SECONDS`: default 60
  - `CVX_RSA_SIM_ARRIVED_AFTER_SECONDS`: default 300; must be greater than the EN_ROUTE value
  - `CVX_RSA_REQUESTED_STALE_AFTER_SECONDS`: default 900; must be ≥ 600
- Optional for the API stack: `CVX_RSA_POSITION_MAX_AGE_SECONDS`, default 900, `/escalate`'s position
  freshness gate. It must be a whole number of seconds from 1 to 3600. Three places check it:
  - `make _deploy-cdk` (which `make deploy-staging` and `make deploy` both run) refuses anything else;
  - the API stack's synth throws on a literal outside that range, so a bare `cdk deploy` fails too;
  - the handler refuses **every** position if the value it receives is not a finite number in
    (0, 3600], including `nan`, `inf` and `1e9`. Roadside requests stop dispatching; the human
    handoff is unaffected.

  Before Fix Group 13 (escalation spec, 2026-09-25) `nan`, `inf` or a huge value turned the check
  off and let a stale position dispatch (`issues/2026-09-25-rsa-position-max-age-accepts-non-finite/`).
  The ceiling is one number in three files, and `lambdas/api-escalate/tests` fails if they differ.
  <!-- verify: grep -n "_RSA_POSITION_MAX_AGE_CEILING_SECONDS = \|RSA_POSITION_MAX_AGE_CEILING_SECONDS = \|-le 3600" lambdas/api-escalate/handler.py infrastructure/lib/vsa-api-stack.ts Makefile -->
- Lambda assets staged. `cdk` synthesizes every stack in the app, so every `build/` directory must
  exist, not only the RSA ones. `scripts/build-rsa-lambda.sh` copies `lambdas/shared/rsa_client*.py`
  into `build/api-escalate` and `build/rsa-dispatch-consumer`. Without it both Lambdas fail at cold
  start, and the API stack's staged-asset check fails synth on a stale copy.
- No Docker is needed; the Python assets are pre-staged directories.
- On a build host whose `python3` is 3.14, `build-triage-classifier.sh` fails (`pyyaml==6.0.2` has no
  cp314 wheel). The triage stack is not part of this deploy, and the directory the script creates
  before failing is enough for synth.

### Deploy (both stacks, nothing else)

```bash
# From the repo root. Stage every Lambda asset the app synthesizes.
bash scripts/build-avx-lambda.sh
bash scripts/build-rsa-lambda.sh
bash scripts/build-api-vehicle-live-state.sh
bash scripts/build-api-ws-authorizer.sh
bash scripts/build-triage-classifier.sh

cd infrastructure
../scripts/with-stage-env.sh staging us-west-2 -- \
  env VSA_STAGE=staging AWS_REGION=us-west-2 CVX_RSA_ALARM_EMAIL=none \
  npx cdk deploy vsa-staging-rsa vsa-staging-api --exclusively \
    --require-approval never -c adpKbId=BY3LVWADHB
```

- `--exclusively` is required. Without it CDK also deploys every stack the API stack depends on.
- The RSA stack deploys first. The API stack imports the table's key ARN from it, and CDK orders the
  two stacks by that dependency.
- **The one-time key switch** (Fix Group 13, 2026-09-25) moves the table from the key in
  `/cvx/{stage}/cms-kms-key-arn` to the stack's own key. `cdk diff` shows the table as `[~]`
  (updated in place, not replaced). Between the RSA stack's update and the API stack's, `/escalate`
  and the status read have no grant on the new key: roadside rows fail to write (logged as
  `rsa-dispatch: put_item failed`) and status reads fail. The chat handoff is unaffected. Deploy both
  stacks in one command so the window is minutes.
- `make deploy-staging` is the full path: every stack plus both AgentCore runtimes. Use it only when
  that is what you intend.
- `cdk deploy` ships the working tree, including uncommitted and untracked files under the bundled
  paths. With other work in the tree, deploy from a clean `git worktree` at the commit you mean.
  Symlink `infrastructure/node_modules` and `.venv` into it, copy `infrastructure/cdk.context.json`,
  and run the build scripts there.
- Run `npx cdk diff vsa-staging-rsa vsa-staging-api --exclusively --no-change-set` with the same
  environment first, and read what else the API stack is carrying.
- **Asset caveat**: `build-api-ws-authorizer.sh` installs an unpinned `PyJWT[crypto]` for the build
  host's Python. A rebuild can change the WebSocket authorizer's asset with no source change. If the
  diff shows `WsAuthorizerFn` code changing and you did not mean it to, reuse the previously built
  `build/api-ws-authorizer`.

### Expected outcome

- `vsa-{stage}-rsa` is CREATE_COMPLETE or UPDATE_COMPLETE, with:
  - table `vsa-{stage}-rsa-dispatch`: PK `dispatchId`, stream NEW_AND_OLD_IMAGES, GSI
    `status-createdAt-index`, PITR on, RETAIN on delete, deletion protection on, SSE-KMS with the
    stack's own customer-managed key `alias/vsa-{stage}-rsa-dispatch` (rotation on, RETAIN).
    Before Fix Group 13 the table used the key in `/cvx/{stage}/cms-kms-key-arn`, which in staging
    names the AWS-managed `alias/aws/dynamodb` key
    (`issues/2026-09-25-rsa-dispatch-table-on-aws-managed-key/`). Check the live table:

    ```bash
    K=$(aws dynamodb describe-table --table-name vsa-staging-rsa-dispatch --region us-west-2 \
          --query 'Table.SSEDescription.KMSMasterKeyArn' --output text)
    aws kms describe-key --key-id "$K" --region us-west-2 --query 'KeyMetadata.KeyManager'   # CUSTOMER
    aws dynamodb describe-table --table-name vsa-staging-rsa-dispatch --region us-west-2 \
      --query 'Table.DeletionProtectionEnabled'                                              # true
    ```
    <!-- verify: grep -n "alias: \`alias/\${p}-rsa-dispatch\`\|deletionProtection: true\|enableKeyRotation: true" infrastructure/lib/vsa-rsa-stack.ts -->
  - Lambdas `vsa-{stage}-rsa-dispatch-consumer` and `vsa-{stage}-rsa-lifecycle-advancer`;
  - DLQ `vsa-{stage}-rsa-dispatch-dlq`;
  - schedule `vsa-{stage}-rsa-lifecycle-advancer-{region}`, rate 1 minute;
    <!-- verify: grep -n "ScheduleExpression.rate" infrastructure/lib/vsa-rsa-stack.ts -->
  - SNS topic `vsa-{stage}-rsa-operator-alerts`;
  - four alarms on that topic: `vsa-{stage}-rsa-dispatch-dlq-depth`,
    <!-- verify: grep -n "alarmName:" infrastructure/lib/vsa-rsa-stack.ts -->
    `-rsa-lifecycle-advancer-errors`, `-rsa-lifecycle-advancer-not-running` (heartbeat), and
    `-rsa-reconciliation-detected`.
- `vsa-{stage}-api` is UPDATE_COMPLETE, with Lambda `vsa-{stage}-api-rsa-dispatch`, route
  `GET /rsa/dispatch/{dispatchId}` behind the Cognito authorizer, and `vsa-{stage}-api-escalate`
  carrying `RSA_DISPATCH_TABLE=vsa-{stage}-rsa-dispatch`.

### Post-deploy smoke test

```bash
.venv/bin/python3 scripts/smoke-test-rsa-dispatch.py --stage staging --region us-west-2
```

Read-only. It exits non-zero if any of these fails:

- the stacks are complete;
- the consumer, advancer and status-read Lambdas are Active, and the consumer and status-read
  Lambdas run on `VSA_RSA_CLIENT=fake`;
- the consumer's stream mapping is Enabled;
- `/escalate` points at the table;
- the four alarms exist with actions enabled, and each alarm's actions include the RSA operator topic;
- the advancer ran in the last 5 minutes with `errors=0`;
  <!-- verify: grep -n "time.time() - 300" scripts/smoke-test-rsa-dispatch.py -->
- the status route returns 401 without a token.

It prints the topic's subscription count; 0 means nobody is paged.

For the end-to-end check, which writes and deletes test rows, opens and stops one Connect chat, and
forces one alarm:

```bash
LIVE_TESTS=1 .venv/bin/python3 scripts/live-verify-rsa-dispatch.py
```

- It refuses to run `/escalate` against a tenant with a callback flow configured. Since escalation
  Fix Group 12 it presents the refusal vehicle's assigned driver (the P0 gate requires one), and it
  refuses a driver whose phone is not a fictional `555-01xx` number, so no real phone can ring.
- It exits 1 on any failed check or if it cannot resolve the stack.
- It writes and deletes table rows with your own credentials, so since Fix Group 13 those need
  `kms:Decrypt` on `alias/vsa-{stage}-rsa-dispatch` as well as the DynamoDB actions. Admin
  credentials have it through the key's IAM-delegating policy; a principal with only DynamoDB
  permissions gets `AccessDeniedException`, which is the control working.
- Reference run: 27/27 PASS.

### Tear-down

- **The API stack imports the RSA stack's table exports and its key ARN.** CloudFormation refuses to
  delete `vsa-{stage}-rsa` while `vsa-{stage}-api` references them. Removing that wiring is a **code
  change, not only a stack edit**: `lambdas/api-escalate/handler.py` reads `RSA_DISPATCH_TABLE` when
  it loads, so an API stack without it makes every `/escalate` call fail, including the P0 handoff to
  a human. First change the handler so it no longer needs the variable (make it optional, or drop the
  roadside write). Then remove the wiring from the API stack: the `rsaDispatchTable` and
  `rsaDispatchKey` props, and the `RsaDispatchKmsAccess` and `RsaDispatchKmsDecrypt` grants. Deploy
  both together, and confirm `/escalate` still works. Only then destroy the RSA stack.
- **Always pass `--exclusively`, and never `--force`.** Without `--exclusively`, `cdk destroy`
  selects every stack that depends on the named one, so it would delete `vsa-{stage}-api` (every CVX
  REST route, including `/escalate`) first. `--force` would skip the prompt that lists it. From
  `infrastructure/`, with the same `with-stage-env.sh` wrapper and environment as the deploy:

  ```bash
  npx cdk destroy vsa-staging-rsa --exclusively
  ```

  Read the stack list in the confirmation prompt before answering.
- The table has `RemovalPolicy.RETAIN` and deletion protection, and survives. To delete it, after
  exporting anything you need, record its key ARN (the alias is **not** retained, so `cdk destroy`
  deletes it and nothing else names the key afterwards), then turn deletion protection off:

  ```bash
  aws dynamodb describe-table --table-name vsa-staging-rsa-dispatch --region us-west-2 \
    --query 'Table.SSEDescription.KMSMasterKeyArn' --output text     # keep this ARN
  aws dynamodb update-table --table-name vsa-staging-rsa-dispatch --region us-west-2 \
    --no-deletion-protection-enabled
  aws dynamodb delete-table --table-name vsa-staging-rsa-dispatch --region us-west-2
  ```
- The table's key is RETAIN. Schedule its deletion, by the ARN you recorded, only after the table
  **and its PITR backups** are gone: without the key they cannot be read.
- Lambda log groups created by `Custom::LogRetention` also survive `cdk destroy`.
- The five historical `PENDING` roadside rows in `cms-{stage}-vfo-action-queue` (a CMS table that is
  still active; VFO, the system it served, was retired) are left as they are. They are not dispatch
  records and are not migrated.
  <!-- verify: aws dynamodb scan --table-name cms-staging-vfo-action-queue --region us-west-2 --filter-expression "actionType = :t" --expression-attribute-values '{":t":{"S":"ROADSIDE_DISPATCH"}}' --select COUNT -->

### Troubleshooting

- **Both Lambdas raise `No module named 'shared'` at cold start.** `build-rsa-lambda.sh` did not
  run before the deploy. Re-stage and redeploy.
- **`/escalate` logs `rsa-dispatch: put_item failed` with a KMS `AccessDeniedException`, or the
  status read returns 503.** The RSA stack is on the new key and the API stack is not yet
  redeployed, so its roles have no grant on it. Deploy `vsa-{stage}-api`.
- **Every `/escalate` roadside request ends FAILED with "Dispatch location (lat/lon) absent".** No
  fresh vehicle position: live-state must report `locationSource: redis` with `lastSeenAgoSeconds`
  ≤ `RSA_POSITION_MAX_AGE_SECONDS`. That needs live telemetry, for example the CMS simulator. This is
  the fail-closed path working, not a fault.
- **The reconciliation alarm is in ALARM.** The advancer reconciled a REQUESTED row older than the
  stale threshold to FAILED. Check the DLQ (`vsa-{stage}-rsa-dispatch-dlq`) and the consumer's logs.
  The alarm returns to OK after a 5-minute period with no reconciliations.
  <!-- verify: grep -n "period: cdk.Duration.minutes\|evaluationPeriods" infrastructure/lib/vsa-rsa-stack.ts -->
- **The heartbeat alarm (`-not-running`) is in ALARM.** The scheduler did not invoke the advancer for
  10 minutes (two 5-minute periods): the schedule is disabled, its role is missing, or its target is
  wrong.
