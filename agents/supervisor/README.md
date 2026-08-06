# VSA Supervisor Agent

Strands-based supervisor agent for the Virtual Service Agent, running
Claude Sonnet 4.5 on Amazon Bedrock. This is Phase 3, step 1: local
tool-calling verification before deployment to AgentCore.

## Layout

```
agents/supervisor/
├── supervisor.py      # Agent construction + system prompt
├── config.py          # Env/CLI arg loading (tenantId, JWT, VIN, base URL)
├── session.py         # SessionContext dataclass threaded through tools
├── tools/
│   ├── triage.py      # Calls deployed POST /triage endpoint
│   ├── retrieve.py    # Stub over corpora/kb-fixtures/
│   └── book.py        # Stub emitting STAR-shaped ServiceRequestAdd envelope
├── cli.py             # python -m agents.supervisor.cli
└── tests/
    ├── test_tools_unit.py
    ├── test_scenarios.py
    └── scenarios/
        ├── p3_tire_pressure.json
        ├── p1_coolant_overtemp.json
        └── p0_brake_fault.json
```

## Prerequisites

1. **Python 3.12.** Same version as the triage classifier Lambda.
2. **AWS credentials** for your target account, us-east-1, with
   `bedrock:InvokeModel` for `anthropic.claude-sonnet-4-6`.
   Default credential chain — use `aws sso login` or set `AWS_PROFILE`.
3. **Cognito auth cached** at `~/.vsa/tokens.json`. First run of the CLI
   will prompt for a demo persona username and that user's password, then cache
   the token.

   There is no shared demo password — each account has its own, stored in
   Secrets Manager as a JSON map keyed by email in
   `vsa-<stage>-michelin-demo-passwords`:

   ```bash
   aws secretsmanager get-secret-value \
     --secret-id vsa-<stage>-michelin-demo-passwords \
     --region <stage-region> --query SecretString --output text \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["<your-email>"])'
   ```

   Or re-issue one by running `scripts/seed-persona-users.py`, which prints the
   freshly generated password for the account it creates.

   **The stages are not symmetric.** `demo@michelin.example` is seeded by
   `scripts/seed-michelin-user.py`, which defaults to `STAGE=prod`, so it exists
   in the **prod** tenant pool only and its password lives in the prod secret.
   The **staging** pool is seeded with the persona accounts instead
   (`ford.driver@example.com`, `enterprise.driver@example.com`,
   `service.advisor@example.com`, `technician@example.com`,
   `vehicle.owner@example.com`, `samantha.carter@example.com`, ...). Prefer a
   staging persona for day-to-day dev rather than signing in as a production
   account. Background: `issues/2026-08-03-cvx-demo-password-rotation/`.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r agents/supervisor/requirements.txt
```

## Quick start

```bash
# Interactive chat session against Michelin tenant
python -m agents.supervisor.cli

# Run a scripted scenario
python -m agents.supervisor.cli --scenario=p1_coolant_overtemp

# Run all three scenarios and assert tool sequences
pytest agents/supervisor/tests/test_scenarios.py -v
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `VSA_API_BASE` | `https://jsy5rf5c2b.execute-api.us-east-1.amazonaws.com/prod` | REST API base |
| `VSA_TENANT_ID` | `michelin` | Tenant for this session |
| `VSA_COGNITO_POOL_ID` | `<user-pool-id>` | Cognito pool for this tenant |
| `VSA_COGNITO_CLIENT_ID` | `3alnr8fdjp1o4vb3rmvheircli` | Michelin Cognito client |
| `VSA_DEMO_USERNAME` | `demo@michelin.example` | Dev sign-in. **Prod-pool only** — see Prerequisites; use a staging persona against staging |
| `VSA_DEMO_PASSWORD` | _(none — set it)_ | Dev sign-in. No default and no shared value; retrieve per-user from `vsa-<stage>-michelin-demo-passwords` or re-issue via `scripts/seed-persona-users.py` |
| `VSA_DEFAULT_VIN` | `1HGBH41JXMN000024` | VIN to seed into session context (matches CMS VEH-0025) |
| `VSA_DEFAULT_VEHICLE_ID` | `VEH-0025` | CMS vehicleId for session/book() writes |
| `VSA_DEFAULT_DRIVER_ID` | `DRV-0055` | CMS driverId for session/book() writes |
| `VSA_KB_FIXTURES_DIR` | `corpora/kb-fixtures` | Where retrieve tool reads from |
| `AWS_REGION` | `us-east-1` | Bedrock region |

## Required AgentCore custom-header allowlist

The bidi runtime needs these custom headers whitelisted in
`.bedrock_agentcore.yaml` under the agent's
`request_header_configuration.requestHeaderAllowlist`:

```yaml
requestHeaderAllowlist:
  - X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token
  - X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id
  - X-Amzn-Bedrock-AgentCore-Runtime-Custom-Vin
  - X-Amzn-Bedrock-AgentCore-Runtime-Custom-VehicleId
  - X-Amzn-Bedrock-AgentCore-Runtime-Custom-DriverId
```

AgentCore silently drops any custom header not on this list — if book()
returns `draft-created` instead of `sink-accepted` after a session, the
first thing to check is that VehicleId + DriverId are on the allowlist.
The `.bedrock_agentcore.yaml` file itself is gitignored because it
contains per-developer runtime IDs, so each dev needs to add these
headers locally after running `agentcore configure`.

## What this is NOT

- Not a production agent. The `book` tool is a stub that does not write to
  any sink. The `retrieve` tool is a local-fixture stub.
- Not deployed. This is local-dev-only until Phase 3 step 2.
- Not the voice interface. Nova Sonic wrapping comes after local
  tool-calling is proven.

## Deploying to AgentCore Runtime (Phase 3 step 2)

The `agentcore_app.py` entrypoint and `agentcore_requirements.txt` are
the deployment artifacts. The AgentCore starter toolkit CLI manages the
runtime itself (outside CDK for now).

### Prerequisites

```bash
pip install bedrock-agentcore-starter-toolkit
```

Observability requires CloudWatch Transaction Search enabled in the
account — do that once per account before the first deploy.

### First deploy

```bash
# From the repo root.
agentcore configure -e agents/supervisor/agentcore_app.py -r us-east-1
# Accept defaults unless you have specific needs. Choose --disable-memory
# for now; session state is stateless per invocation.
agentcore configure -e agents/supervisor/agentcore_app.py -r us-east-1 --disable-memory

# Attach Bedrock model permissions to the execution role the toolkit
# created. Idempotent — safe to re-run.
python scripts/attach-agentcore-permissions.py

# Package + deploy via CodeBuild. Default mode is direct_code_deploy
# (zip + S3). Takes 2-4 min on first deploy.
agentcore deploy
```

### Invoke

```bash
# Quick smoke test via the CLI. The toolkit persists your session id
# in .bedrock_agentcore.yaml between runs.
agentcore invoke '{
  "prompt": "Temperature gauge just spiked.",
  "tenantId": "michelin",
  "vin": "1FMCU0F70MUA12345"
}'
```

The CLI does not pass custom headers, so this invocation fails auth
(`triage` tool returns 401). For a real end-to-end check, use the
boto3 client with a Cognito IdToken in the Authorization header —
see `tests/test_agentcore_app.py::test_full_invoke_live` for the
direct-entrypoint version, and the Phase 3 step 3 Nova Sonic wrapper
for the HTTP-with-headers version.

### Subsequent deploys

```bash
agentcore deploy   # just re-deploys the latest code
```

### Teardown

```bash
agentcore destroy   # deletes the runtime + ECR repo + S3 bucket
```

The starter toolkit does NOT delete the inline policy added by
`scripts/attach-agentcore-permissions.py`. Remove it manually if you're
tearing down permanently:

```bash
aws iam delete-role-policy \
    --role-name <execution-role-name> \
    --policy-name VsaAgentCoreSupervisorBedrockAccess
```

### Where things live

| Resource | Location |
|---|---|
| Agent code | `agents/supervisor/agentcore_app.py` + imports |
| Runtime config | `.bedrock_agentcore.yaml` (gitignored, dev-local) |
| Deployment bucket | S3 (created by toolkit; see `agentcore status`) |
| Execution role | IAM; ARN in `.bedrock_agentcore.yaml` |
| Agent runtime ARN | `.bedrock_agentcore.yaml` → `bedrock_agentcore.agent_arn` |
| CloudWatch logs | `/aws/bedrock-agentcore/runtimes/{agent-id}-DEFAULT` |
