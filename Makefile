STAGE ?= prod
AWS_REGION ?= us-east-1
VENV_PYTHON := .venv/bin/python3

.PHONY: install synth diff deploy deploy-staging _deploy-cdk seed validate-schema test clean agent-inventory agent-inventory-check demo-preflight preflight verify-kiosk-runtime

install:
	cd infrastructure && npm install
	python3 -m pip install -q --user boto3 jsonschema

# synth / diff resolve Tier B coordinates from SSM the same way `_deploy-cdk` does.
# The `:-prod` / `:-us-east-1` fallbacks deliberately mirror bin/vsa.ts's own
# defaults (`process.env.VSA_STAGE || 'prod'`, `process.env.AWS_REGION || 'us-east-1'`)
# so the stage these targets act on is unchanged by this wiring — only the source of
# the Tier B values changed.
synth:
	cd infrastructure && ../scripts/with-stage-env.sh "$${VSA_STAGE:-prod}" "$${AWS_REGION:-us-east-1}" -- npm run synth

diff:
	cd infrastructure && ../scripts/with-stage-env.sh "$${VSA_STAGE:-prod}" "$${AWS_REGION:-us-east-1}" -- npm run diff

# Internal: CDK-only deploy. Composed by `deploy` and `deploy-staging`.
# STAGE and AWS_REGION drive STAGE_DEFAULTS lookup in infrastructure/bin/vsa.ts.
# CDK_CONTEXT_ARGS is an optional passthrough for extra `-c key=val` context
# (deploy-staging uses it to thread `-c adpKbId=<KB_ID>`; prod passes nothing,
# so the vsa-core-stack KB grant falls back to the wildcard ARN).
# Tier B per-stage coordinates are exported from SSM by `with-stage-env.sh` before
# cdk starts. They used to be baked into `infrastructure/bin/vsa.ts` as
# STAGE_DEFAULTS literals; spec `2026-08-05-cvx-stage-defaults-parameterization`
# moved them out because bin/vsa.ts is the app entrypoint and therefore cannot be
# excluded from a public publish. Without the wrapper this target fails on the
# fail-closed guard in bin/vsa.ts, which is the intended behaviour when a value is
# genuinely unavailable — the wrapper is what makes it available.
#
# Seed a stage's parameters first: scripts/seed-stage-parameters.sh --stage <stage> --apply
# Tier A (cms-kms-key-arn, redis-endpoint, assistant-chat-runtime-arn) is NOT
# exported here — CloudFormation resolves those from the same parameters at deploy.
_deploy-cdk:
	cd infrastructure && ../scripts/with-stage-env.sh $(STAGE) $(AWS_REGION) -- \
	  env VSA_STAGE=$(STAGE) AWS_REGION=$(AWS_REGION) npm run deploy -- --all --require-approval never $(CDK_CONTEXT_ARGS)

# Full prod deploy: CDK stacks (us-east-1) + AgentCore vsa_supervisor_bidi
# runtime + (conditionally) the vsa_supervisor_text_prod text runtime used
# by the CMS-prod web UI's /assistant/chat route (spec
# 2026-07-16-cvx-prod-cutover). Mirrors deploy-staging's pre-flight +
# CFN-output-resolution + conditional-text-runtime pattern. AgentCore
# deploy is idempotent via --auto-update-on-conflict. Runtime env vars
# (CMS_STAGE, VSA_STAGE, AWS_REGION) are passed via --env so the agent
# code reads the correct prod tables.
# Spec: .kiro/specs/2026-05-28-agentcore-staging-infra/ (bidi runtime)
#       .kiro/specs/2026-07-16-cvx-prod-cutover/ (text runtime + pre-flight)
#
# NOTE on VSA_ADP_ACCOUNT_ID: it deliberately defaults to EMPTY, not to a
# placeholder. `Config.from_env()` falls back to sts:GetCallerIdentity when the
# value is empty, which resolves the real account. This target previously
# defaulted it to 123456789012 (the AWS-docs placeholder account): being
# non-empty, that SUPPRESSED the STS fallback and built cross-account KB ARNs
# against an account that does not exist, so KB-grounded tools on prod would
# fail or silently fall back to local corpora. A placeholder that can never be
# correct is worse than empty. Staging already passed it through empty.
deploy:
	@aws sts get-caller-identity > /dev/null 2>&1 \
	  || (echo "❌ AWS credentials not configured"; exit 1)
	@command -v agentcore > /dev/null 2>&1 \
	  || (echo "❌ agentcore CLI not found — run: pipx install bedrock-agentcore-starter-toolkit"; exit 1)
	@echo "✅ Pre-flight passed"
	@# Kiosk-hardening gate (security-review Cycle 7 S5). Staging has run this
	@# since Group 4; prod did not, which meant the riskier of the two
	@# environments had the weaker gate. `preflight` is fast and offline.
	$(MAKE) preflight
	$(MAKE) _deploy-cdk STAGE=prod AWS_REGION=us-east-1
	agentcore deploy -a vsa_supervisor_bidi --auto-update-on-conflict \
	  --env CMS_STAGE=prod \
	  --env VSA_STAGE=prod \
	  --env AWS_REGION=us-east-1
	@# ── Text runtime (vsa_supervisor_text_prod) — HTTP InvokeAgentRuntime
	@#    path used by the CMS-prod web UI's /assistant/chat route (spec
	@#    2026-07-16-cvx-prod-cutover). Same env var set as bidi (sans the
	@#    voice-only vars). Conditional on the operator having configured
	@#    the agent locally (one-time setup — see docs/DEPLOYMENT.md §
	@#    "Component 1c"). If absent, skips with a clear message so the
	@#    bidi deploy above is the only one that runs.
	@if grep -q "name: vsa_supervisor_text_prod" .bedrock_agentcore.yaml 2>/dev/null; then \
	  echo "ℹ️  Deploying text runtime (vsa_supervisor_text_prod) — used by the /assistant/chat route"; \
	  MEMORY_ID="$${VSA_FOUNDATION_MEMORY_ID:-$$(aws cloudformation describe-stacks --stack-name vsa-prod-foundation --region us-east-1 --query \"Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue\" --output text 2>/dev/null)}"; \
	  if [ -z "$$MEMORY_ID" ] || [ "$$MEMORY_ID" = "None" ]; then \
	    echo "❌ Could not resolve VSA_FOUNDATION_MEMORY_ID."; \
	    echo "   Set the env var, or ensure the vsa-prod-foundation stack exports"; \
	    echo "   the 'MemoryId' output. Without it the runtime raises"; \
	    echo "   'VSA_FOUNDATION_MEMORY_ID is required but not set' on every persona session."; \
	    exit 1; fi; \
	  API_BASE="$${VSA_API_BASE:-$$(aws cloudformation describe-stacks --stack-name vsa-prod-api --region us-east-1 --query \"Stacks[0].Outputs[?OutputKey=='RestApiUrl'].OutputValue\" --output text 2>/dev/null)}"; \
	  API_BASE="$${API_BASE%/}"; \
	  if [ -z "$$API_BASE" ] || [ "$$API_BASE" = "None" ]; then \
	    echo "❌ Could not resolve VSA_API_BASE."; \
	    echo "   Set the env var, or ensure the vsa-prod-api stack exports the"; \
	    echo "   'RestApiUrl' output. Without it config.api_base is empty and the"; \
	    echo "   triage tool builds an unscheme'd URL that raises requests.MissingSchema."; \
	    exit 1; fi; \
	  KIOSK_GUARDRAIL_ID="$${VSA_KIOSK_GUARDRAIL_ID:-$$(aws cloudformation describe-stacks --stack-name vsa-prod-core --region us-east-1 --query \"Stacks[0].Outputs[?OutputKey=='KioskGuardrailId'].OutputValue\" --output text 2>/dev/null)}"; \
	  if [ -z "$$KIOSK_GUARDRAIL_ID" ] || [ "$$KIOSK_GUARDRAIL_ID" = "None" ]; then \
	    echo "❌ Could not resolve VSA_KIOSK_GUARDRAIL_ID for PROD."; \
	    echo "   vsa-prod-core must be deployed first and export 'KioskGuardrailId'."; \
	    echo "   This guard exists because the staging target shipped without it and"; \
	    echo "   KioskGuardrailEnforcer silently DISABLED itself — every visitor prompt"; \
	    echo "   reached the model unfiltered, returning 200, with no failure signal."; \
	    echo "   On a public show floor that is an unbounded LLM with no content policy."; \
	    echo "   See issues/2026-08-02-text-runtime-env-unguarded-and-kiosk-guardrail-unwired/"; \
	    echo "   (security-review Cycle 5 W1 — prod-cutover blocker)."; \
	    exit 1; fi; \
	  echo "ℹ️  Foundation Memory ID: $$MEMORY_ID"; \
	  echo "ℹ️  API base: $$API_BASE"; \
	  echo "ℹ️  Kiosk guardrail ID: $$KIOSK_GUARDRAIL_ID"; \
	  agentcore deploy -a vsa_supervisor_text_prod --auto-update-on-conflict \
	    --env CMS_STAGE=prod \
	    --env VSA_STAGE=prod \
	    --env AWS_REGION=us-east-1 \
	    --env VSA_CMS_REGION=us-east-1 \
	    --env VSA_FOUNDATION_MEMORY_ID=$$MEMORY_ID \
	    --env VSA_API_BASE=$$API_BASE \
	    --env VSA_ADP_KB_ID=$${VSA_ADP_KB_ID:-} \
	    --env VSA_ADP_REGION=$${VSA_ADP_REGION:-us-east-1} \
	    --env VSA_ADP_ACCOUNT_ID=$${VSA_ADP_ACCOUNT_ID:-} \
	    --env AGENTCORE_STAGE=prod \
	    --env VSA_KIOSK_GUARDRAIL_ID=$$KIOSK_GUARDRAIL_ID; \
	else \
	  echo "⚠️  Skipping text-runtime deploy — vsa_supervisor_text_prod not in .bedrock_agentcore.yaml."; \
	  echo "    To enable (one-time): agentcore configure --entrypoint agents/supervisor/agentcore_app.py \\"; \
	  echo "      --name vsa_supervisor_text_prod \\"; \
	  echo "      --execution-role arn:aws:iam::123456789012:role/AmazonBedrockAgentCoreSDKRuntime-us-east-1-prod \\"; \
	  echo "      --region us-east-1 --deployment-type direct_code_deploy --runtime PYTHON_3_12 \\"; \
	  echo "      --protocol HTTP --disable-otel --non-interactive"; \
	  echo "    Then patch .bedrock_agentcore.yaml entrypoint+source_path to relative paths"; \
	  echo "    (see docs/DEPLOYMENT.md § 'Component 1c: CDK + AgentCore prod')."; \
	fi
	@echo ""
	@echo "⚠️  PROD POST-DEPLOY GATE — not automated here on purpose."
	@echo "    The prod text-runtime id is per-deployment, and guessing it would"
	@echo "    produce a spurious pass/fail. Run this before opening public kiosk"
	@echo "    traffic, with the id from 'agentcore status':"
	@echo ""
	@echo "      make verify-kiosk-runtime KIOSK_STAGE=prod KIOSK_REGION=us-east-1 \\"
	@echo "        KIOSK_RUNTIME_ID=<vsa_supervisor_text_prod-XXXXXXXX>"
	@echo ""
	@echo "    Then confirm the guardrail actually BLOCKS with a live off-topic"
	@echo "    prompt AND a legitimate positive control — configuration being"
	@echo "    present is not the same as enforcement working."
	@echo "    docs/DEPLOYMENT.md § Guardrail post-deploy verification (Zone 2)."

# Full staging deploy: CDK stacks (us-west-2) + AgentCore
# vsa_supervisor_bidi_staging runtime + iOS Staging.xcconfig back-fill of
# VSA_AGENTCORE_RUNTIME_ARN.
#
# Flow:
#   1. CDK deploy (creates AgentCore execution role + S3 bucket as part
#      of vsa-staging-core stack, alongside the four DDB tables)
#   2. patch-agentcore-yaml.py: read CFN outputs, write into
#      .bedrock_agentcore.yaml::agents.vsa_supervisor_bidi_staging.aws
#   3. agentcore deploy: build container, push, create/update runtime in
#      us-west-2 with staging env vars. NOVA_SONIC_REGION=us-west-2 (native):
#      Nova Sonic v2 is AUTHORIZED/AVAILABLE in us-west-2, and the bidi-stream
#      allowlist requires the invocation region to match the execution-role
#      region (AmazonBedrockAgentCoreSDKRuntime-us-west-2-*). Cross-region
#      us-east-1 invocation with a us-west-2 role is denied — see
#      issues/2026-06-02-staging-nova-sonic-us-west-2-access-denied.
#   4. update-staging-xcconfig.py: read deployed runtime ARN from yaml,
#      patch into CMS repo's Staging.xcconfig (no-op when CMS repo is
#      not present, e.g. in CI).
#
# Auto-invoked by .github/workflows/evals.yml on push to main after
# Tier 1/2/3 evals pass. Spec: .kiro/specs/2026-05-28-agentcore-staging-infra/
deploy-staging:
	@aws sts get-caller-identity > /dev/null 2>&1 \
	  || (echo "❌ AWS credentials not configured"; exit 1)
	@command -v agentcore > /dev/null 2>&1 \
	  || (echo "❌ agentcore CLI not found — run: pipx install bedrock-agentcore-starter-toolkit"; exit 1)
	@if [ -z "$$VSA_ADP_KB_ID" ]; then \
	  echo "❌ VSA_ADP_KB_ID is required for staging deploy (spec 2026-06-18-cvx-adp-kb-wireup)."; \
	  echo "   Without it the AgentCore runtime gets adp_kb_id=\"\" and KB-grounded tools"; \
	  echo "   silently fall back to local corpora. Run e.g.:"; \
	  echo "     VSA_ADP_KB_ID=YULHRAUMNU make deploy-staging"; \
	  exit 1; fi
	@if ! printf '%s' "$$VSA_ADP_KB_ID" | grep -qE '^[A-Z0-9]{8,16}$$'; then \
	  echo "❌ VSA_ADP_KB_ID='$$VSA_ADP_KB_ID' is not a valid Bedrock KB id"; \
	  echo "   (expected 8-16 upper-alphanumeric chars, e.g. YULHRAUMNU)."; \
	  echo "   This guard blocks a wildcard or malformed value (e.g. '*') from"; \
	  echo "   threading into '-c adpKbId=...' and re-widening the cross-account"; \
	  echo "   IAM grant to knowledge-base/* — the fail-closed tightening from"; \
	  echo "   spec 2026-06-18-cvx-adp-kb-wireup decisions.md."; \
	  exit 1; fi
	@echo "✅ Pre-flight passed"
	$(MAKE) _deploy-cdk STAGE=staging AWS_REGION=us-west-2 CDK_CONTEXT_ARGS="-c adpKbId=$(VSA_ADP_KB_ID)"
	$(VENV_PYTHON) scripts/patch-agentcore-yaml.py \
	  --agent vsa_supervisor_bidi_staging \
	  --stack vsa-staging-core \
	  --region us-west-2
	@NOVA_SONIC_REGION=us-west-2; \
	MEMORY_ID="$${VSA_FOUNDATION_MEMORY_ID:-$$(aws cloudformation describe-stacks --stack-name vsa-staging-foundation --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue" --output text 2>/dev/null)}"; \
	if [ -z "$$MEMORY_ID" ] || [ "$$MEMORY_ID" = "None" ]; then \
	  echo "❌ Could not resolve VSA_FOUNDATION_MEMORY_ID."; \
	  echo "   Set the env var, or ensure the vsa-staging-foundation stack exports"; \
	  echo "   the 'MemoryId' output. Without it the runtime raises"; \
	  echo "   'VSA_FOUNDATION_MEMORY_ID is required but not set' on every persona"; \
	  echo "   session (issues/2026-06-19-staging-runtime-missing-foundation-memory-id)."; \
	  exit 1; fi; \
	API_BASE="$${VSA_API_BASE:-$$(aws cloudformation describe-stacks --stack-name vsa-staging-api --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='RestApiUrl'].OutputValue" --output text 2>/dev/null)}"; \
	API_BASE="$${API_BASE%/}"; \
	if [ -z "$$API_BASE" ] || [ "$$API_BASE" = "None" ]; then \
	  echo "❌ Could not resolve VSA_API_BASE."; \
	  echo "   Set the env var, or ensure the vsa-staging-api stack exports the"; \
	  echo "   'RestApiUrl' output. Without it config.api_base is empty and the"; \
	  echo "   triage tool builds an unscheme'd URL '/triage' that raises"; \
	  echo "   requests.MissingSchema on every auto-triage"; \
	  echo "   (issues/2026-06-19-cvx-staging-kb-grounding-local-fallback)."; \
	  exit 1; fi; \
	echo "ℹ️  Nova Sonic region: $$NOVA_SONIC_REGION (native; MUST match the execution-role region per the bidi-stream allowlist — cross-region invocation with a us-west-2 role yields empty-message AccessDeniedException; see issues/2026-06-02-staging-nova-sonic-us-west-2-access-denied)"; \
	echo "ℹ️  Foundation Memory ID: $$MEMORY_ID"; \
	echo "ℹ️  API base: $$API_BASE"; \
	agentcore deploy -a vsa_supervisor_bidi_staging --auto-update-on-conflict \
	  --env CMS_STAGE=staging \
	  --env VSA_STAGE=staging \
	  --env AWS_REGION=us-west-2 \
	  --env VSA_CMS_REGION=us-west-2 \
	  --env NOVA_SONIC_REGION=$$NOVA_SONIC_REGION \
	  --env VSA_FOUNDATION_MEMORY_ID=$$MEMORY_ID \
	  --env VSA_API_BASE=$$API_BASE \
	  --env VSA_ADP_KB_ID=$$VSA_ADP_KB_ID \
	  --env VSA_ADP_REGION=$${VSA_ADP_REGION:-us-east-1} \
	  --env VSA_ADP_ACCOUNT_ID=$$VSA_ADP_ACCOUNT_ID \
	  --env VSA_KB_SOURCE_CATEGORY_FILTER=true \
	  --env VSA_ACQUIRE_CATALOG_TABLE=vsa-staging-acquire-catalog \
	  --env VSA_ACQUIRE_ORDERS_TABLE=vsa-staging-acquire-orders \
	  --env VSA_ACQUIRE_LEADS_TABLE=vsa-staging-acquire-leads \
	  --env VSA_DISCOVER_KB_SOURCE_CATEGORY=vehicle_shopping_guide \
	  --env VSA_ACQUIRE_KB_SOURCE_CATEGORY=vehicle_manufacturing \
	  --env VSA_DISCOVER_GUARDRAILS_ID=discover-offers-guardrails-v1

	@# ── Text runtime (vsa_supervisor_text_staging) — HTTP InvokeAgentRuntime
	@#    path used by the CMS web UI's /assistant/chat route (spec
	@#    2026-06-19-cvx-web-ui-surfacing). Same env var set as bidi (sans
	@#    NOVA_SONIC_REGION which is voice-only). Conditional on the
	@#    operator having configured the agent locally (one-time setup —
	@#    see docs/DEPLOYMENT.md). If absent, skips with a warning so the
	@#    bidi deploy is the only one that runs (legacy behavior preserved).
	@if grep -q "name: vsa_supervisor_text_staging" .bedrock_agentcore.yaml 2>/dev/null; then \
	  echo "ℹ️  Deploying text runtime (vsa_supervisor_text_staging) — used by the /assistant/chat route"; \
	  MEMORY_ID="$${VSA_FOUNDATION_MEMORY_ID:-$$(aws cloudformation describe-stacks --stack-name vsa-staging-foundation --region us-west-2 --query \"Stacks[0].Outputs[?OutputKey=='MemoryId'].OutputValue\" --output text 2>/dev/null)}"; \
	  if [ -z "$$MEMORY_ID" ] || [ "$$MEMORY_ID" = "None" ]; then \
	    echo "❌ Could not resolve VSA_FOUNDATION_MEMORY_ID for the TEXT runtime."; \
	    echo "   The text runtime previously deployed with the literal string 'None' here"; \
	    echo "   because this block had no guard (issues/2026-08-02-text-runtime-env-unguarded-and-kiosk-guardrail-unwired)."; \
	    exit 1; fi; \
	  API_BASE="$${VSA_API_BASE:-$$(aws cloudformation describe-stacks --stack-name vsa-staging-api --region us-west-2 --query \"Stacks[0].Outputs[?OutputKey=='RestApiUrl'].OutputValue\" --output text 2>/dev/null)}"; \
	  API_BASE="$${API_BASE%/}"; \
	  if [ -z "$$API_BASE" ] || [ "$$API_BASE" = "None" ]; then \
	    echo "❌ Could not resolve VSA_API_BASE for the TEXT runtime."; \
	    echo "   Same unguarded-copy defect as VSA_FOUNDATION_MEMORY_ID above."; \
	    exit 1; fi; \
	  KIOSK_GUARDRAIL_ID="$${VSA_KIOSK_GUARDRAIL_ID:-$$(aws cloudformation describe-stacks --stack-name vsa-staging-core --region us-west-2 --query \"Stacks[0].Outputs[?OutputKey=='KioskGuardrailId'].OutputValue\" --output text 2>/dev/null)}"; \
	  if [ -z "$$KIOSK_GUARDRAIL_ID" ] || [ "$$KIOSK_GUARDRAIL_ID" = "None" ]; then \
	    echo "❌ Could not resolve VSA_KIOSK_GUARDRAIL_ID."; \
	    echo "   vsa-staging-core must be deployed first and export 'KioskGuardrailId'."; \
	    echo "   Without it KioskGuardrailEnforcer is INERT on the kiosk text path:"; \
	    echo "   off-topic prompts are answered by the prompt alone, with no"; \
	    echo "   ApplyGuardrail call. Verified live 2026-08-02 — a poem+geography"; \
	    echo "   prompt returned a model deflection, not the canned block message."; \
	    exit 1; fi; \
	  echo "ℹ️  Foundation Memory ID: $$MEMORY_ID"; \
	  echo "ℹ️  API base: $$API_BASE"; \
	  echo "ℹ️  Kiosk guardrail ID: $$KIOSK_GUARDRAIL_ID"; \
	  agentcore deploy -a vsa_supervisor_text_staging --auto-update-on-conflict \
	    --env CMS_STAGE=staging \
	    --env VSA_STAGE=staging \
	    --env AWS_REGION=us-west-2 \
	    --env VSA_CMS_REGION=us-west-2 \
	    --env VSA_FOUNDATION_MEMORY_ID=$$MEMORY_ID \
	    --env VSA_API_BASE=$$API_BASE \
	    --env VSA_ADP_KB_ID=$$VSA_ADP_KB_ID \
	    --env VSA_ADP_REGION=$${VSA_ADP_REGION:-us-east-1} \
	    --env VSA_ADP_ACCOUNT_ID=$$VSA_ADP_ACCOUNT_ID \
	    --env VSA_KB_SOURCE_CATEGORY_FILTER=true \
	    --env VSA_ACQUIRE_CATALOG_TABLE=vsa-staging-acquire-catalog \
	    --env VSA_ACQUIRE_ORDERS_TABLE=vsa-staging-acquire-orders \
	    --env VSA_ACQUIRE_LEADS_TABLE=vsa-staging-acquire-leads \
	    --env VSA_DISCOVER_KB_SOURCE_CATEGORY=vehicle_shopping_guide \
	    --env VSA_ACQUIRE_KB_SOURCE_CATEGORY=vehicle_manufacturing \
	    --env VSA_DISCOVER_GUARDRAILS_ID=discover-offers-guardrails-v1 \
	    --env AGENTCORE_STAGE=staging \
	    --env VSA_KIOSK_GUARDRAIL_ID=$$KIOSK_GUARDRAIL_ID; \
	else \
	  echo "⚠️  Skipping text-runtime deploy — vsa_supervisor_text_staging not in .bedrock_agentcore.yaml."; \
	  echo "    To enable (one-time): agentcore configure --entrypoint agents/supervisor/agentcore_app.py \\"; \
	  echo "      --name vsa_supervisor_text_staging \\"; \
	  echo "      --execution-role arn:aws:iam::123456789012:role/AmazonBedrockAgentCoreSDKRuntime-us-west-2-staging \\"; \
	  echo "      --region us-west-2 --deployment-type direct_code_deploy --runtime PYTHON_3_12 \\"; \
	  echo "      --protocol HTTP --disable-otel --non-interactive"; \
	  echo "    Then patch .bedrock_agentcore.yaml entrypoint+source_path to relative paths"; \
	  echo "    (issues/2026-06-22-assistant-chat-no-text-runtime-container/summary.md)."; \
	fi
	@echo "ℹ️  Skipping scripts/attach-agentcore-permissions.py — the AgentCore"
	@echo "    execution role is CDK-managed (infrastructure/lib/vsa-core-stack.ts"
	@echo "    is the source of truth and already carries all required grants)."
	@echo "    The script is vestigial for CDK deployments and emits a"
	@echo "    MalformedPolicyDocument; see issues/2026-06-16-attach-agentcore-permissions-malformed-policy/."
	@echo "    Run it manually only for legacy auto-created-role setups."
	$(VENV_PYTHON) scripts/update-staging-xcconfig.py
	$(MAKE) seed-staging
	@# Post-deploy: prove the guardrail LANDED on the runtime, not just that the
	@# deploy resolved it. Only meaningful when the text runtime was actually
	@# deployed above, so gate on the same yaml check.
	@if grep -q "name: vsa_supervisor_text_staging" .bedrock_agentcore.yaml 2>/dev/null; then \
	  $(MAKE) verify-kiosk-runtime KIOSK_STAGE=staging KIOSK_REGION=us-west-2; \
	else \
	  echo "ℹ️  Skipping verify-kiosk-runtime — text runtime not registered locally."; \
	fi

seed:
	STAGE=$(STAGE) AWS_REGION=$(AWS_REGION) python3 scripts/seed-michelin.py

# Seed all demo users/data into the staging environment.
# Creates Cognito users + DDB driver/vehicle/tenant-config rows for all three
# personas: Samantha Carter (Michelin fleet), Marcus Reyes (Ford OEM),
# Priya Shah (Enterprise rental).
seed-staging:
	STAGE=staging AWS_REGION=us-west-2 python3 scripts/seed-michelin.py
	STAGE=staging AWS_REGION=us-west-2 python3 scripts/seed-michelin-user.py samantha.carter@example.com
	STAGE=staging AWS_REGION=us-west-2 python3 scripts/seed-persona-users.py
	STAGE=staging AWS_REGION=us-west-2 python3 scripts/seed-personas.py

validate-schema:
	python3 -c "import json, jsonschema; jsonschema.Draft202012Validator.check_schema(json.load(open('schemas/vsa-tenant-config.schema.json'))); print('schema ok')"
	python3 scripts/lib/validate_acquire_catalog.py \
	    scripts/seed_data/acquire_catalog/genericmoto-catalog.json \
	    scripts/seed_data/acquire_catalog/meridian-catalog.json

test: validate-schema
	cd infrastructure && npm run test

clean:
	rm -rf infrastructure/cdk.out infrastructure/node_modules

agent-inventory: ## Regenerate agent tool inventory from source code
	$(VENV_PYTHON) scripts/generate_agent_inventory.py

agent-inventory-check: ## Verify agent inventory is in sync with source code
	$(VENV_PYTHON) scripts/generate_agent_inventory.py --check


# ===== Zone 2 kiosk preflight gate =====
#
# `make preflight` MUST pass before any staging or prod deploy that includes
# the acquire stack (Groups 2 + 4 of spec 2026-07-31-zone2-order-backend-and-kiosk).
#
# WHY THIS GATE EXISTS (decisions.md Q10):
#   `session_manager_or_none()` in agents/supervisor/session.py gates on
#   ctx.jwt — and kiosk devices carry a valid JWT under spec D2.  Deploying
#   the order backend (Group 2) or its CDK infrastructure (Group 4) without
#   the kiosk-hardening runtime (Group 3) would attach real AgentCore Memory
#   long-term memory to anonymous kiosk sessions, and could bleed one
#   visitor's context into the next session on the same device.
#
# WHAT IT CHECKS:
#   - agents/supervisor/tests/test_kiosk_gating.py  — tool allow-list (10 not 26)
#     and session_manager_or_none() returning None for kiosk sessions
#   - lambdas/api-assistant-chat/tests/test_persona_map.py  — kiosk group
#     resolves to consumer, persona override is ignored, claimless input does
#     not default to fleet_driver
#
# Two pre-existing broken collectors are unconditionally excluded so an
# operator never has to remember them:
#   lambdas/shared/tests/test_cms_client_real.py  (live-AWS integration test)
#   lambdas/triage-classifier/tests/test_classifier.py  (model-weight fixture)
#
# Do NOT weaken this target to a warning — a logged warning is ignored during
# show-floor load-in and the ordering hazard is a verified security issue.

preflight:
	@echo "🔒 Zone 2 preflight: verifying kiosk-hardening tests before deploy..."
	$(VENV_PYTHON) -m pytest \
	  agents/supervisor/tests/test_kiosk_gating.py \
	  lambdas/api-assistant-chat/tests/test_persona_map.py \
	  --ignore=lambdas/shared/tests/test_cms_client_real.py \
	  --ignore=lambdas/triage-classifier/tests/test_classifier.py \
	  -v --tb=short
	@echo "✅ Preflight passed — kiosk-hardening tests green. Safe to deploy."


# ===== Zone 2 kiosk POST-deploy verification =====
#
# The deploy-time guards prove the guardrail id was RESOLVED. They do not prove
# it LANDED on the runtime. Those are different failures: `agentcore deploy` can
# succeed while the runtime ends up without the variable (a --env typo, a
# partially-applied update, a hand-patched runtime, a rollback). The gap between
# "resolved" and "landed" is exactly where the 2026-08-02 outage lived — the
# guardrail existed, was configured, cost money, and enforced nothing, and every
# request returned HTTP 200 with no failure signal.
#
# This target closes that gap by reading the DEPLOYED runtime environment back
# and comparing it against the stack's own export. Run it after any deploy that
# touches the core stack or a text runtime, and before opening public traffic.
#
# Usage:
#   make verify-kiosk-runtime                      # staging (default)
#   make verify-kiosk-runtime KIOSK_STAGE=prod KIOSK_REGION=us-east-1 \
#        KIOSK_RUNTIME_ID=vsa_supervisor_text_prod-XXXXXXXX
#
# Issue: issues/2026-08-02-text-runtime-env-unguarded-and-kiosk-guardrail-unwired/
KIOSK_STAGE      ?= staging
KIOSK_REGION     ?= us-west-2
KIOSK_RUNTIME_ID ?= vsa_supervisor_text_staging-yxYZomEgFz

verify-kiosk-runtime:
	@bash scripts/verify-kiosk-runtime.sh \
	  --stage "$(KIOSK_STAGE)" \
	  --region "$(KIOSK_REGION)" \
	  --runtime-id "$(KIOSK_RUNTIME_ID)"


# ===== Demo readiness =====
#
# `make demo-preflight` runs a one-command read-only check of the staging
# surfaces called out in `docs/DEMO-RUNBOOK.md` § 4 (Pre-flight checklist):
# CFN stacks, bidi runtime status, text-agent smoke, KB-retrieve smoke
# (warns if no s3:// citation), and the persona Tier-3 smoke.
#
# The bidi runtime id is auto-resolved from `.bedrock_agentcore.yaml` — the
# runbook uses a placeholder. The harness is non-interactive and exits
# non-zero on any hard-check RED so it is safe to chain in CI/cron.
#
# Required: AWS credentials with read access in us-west-2 + us-east-1, and
# a Cognito JWT (VSA_USER_TOKEN env or a cached ~/.vsa/tokens.json from
# prior login). VSA_ADP_KB_ID defaults to YULHRAUMNU.
demo-preflight:
	bash scripts/demo-preflight.sh


# ===== Eval pipeline (Group 5b) =====
#
# These targets run the eval pipeline tiers and compare against committed
# baselines via the reporter (evals/runner/reporter.py).
#
# Wrapper script: evals/runner/_run_tier.py invokes pytest and emits the
# JSON schema agreed with reporter.py + baselines/*.json.
#
# pytest auto-discovery note: tier{1,2,3}*.py do NOT match the default
# test_*.py pattern, so we invoke them through the wrapper which calls
# pytest with explicit file paths.

.PHONY: eval-tier1 eval-tier2 eval-tier3 eval-all eval-update-baseline

EVAL_OUT := /tmp

eval-tier1: ## Run Tier 1 evals; compare to baseline
	$(VENV_PYTHON) -m evals.runner._run_tier --tier 1 --output $(EVAL_OUT)/eval-tier1.json
	$(VENV_PYTHON) -m evals.runner.reporter --tier 1 \
	  --results $(EVAL_OUT)/eval-tier1.json \
	  --baseline evals/baselines/tier1.json \
	  --output $(EVAL_OUT)/eval-tier1-report.md

eval-tier2: ## Run Tier 2 evals; compare to baseline
	$(VENV_PYTHON) -m evals.runner._run_tier --tier 2 --output $(EVAL_OUT)/eval-tier2.json
	$(VENV_PYTHON) -m evals.runner.reporter --tier 2 \
	  --results $(EVAL_OUT)/eval-tier2.json \
	  --baseline evals/baselines/tier2.json \
	  --output $(EVAL_OUT)/eval-tier2-report.md

eval-tier3: ## Run Tier 3 evals (requires RUNTIME_ID env)
	@if [ -z "$$RUNTIME_ID" ]; then echo 'RUNTIME_ID required for Tier 3'; exit 1; fi
	$(VENV_PYTHON) -m evals.runner._run_tier --tier 3 --output $(EVAL_OUT)/eval-tier3.json
	$(VENV_PYTHON) -m evals.runner.reporter --tier 3 \
	  --results $(EVAL_OUT)/eval-tier3.json \
	  --baseline evals/baselines/tier3.json \
	  --output $(EVAL_OUT)/eval-tier3-report.md

eval-all: eval-tier1 eval-tier2 eval-tier3 ## Run all three tiers

eval-update-baseline: ## Promote latest results to baselines
	cp $(EVAL_OUT)/eval-tier1.json evals/baselines/tier1.json
	cp $(EVAL_OUT)/eval-tier2.json evals/baselines/tier2.json
	@if [ -f $(EVAL_OUT)/eval-tier3.json ]; then cp $(EVAL_OUT)/eval-tier3.json evals/baselines/tier3.json; fi
