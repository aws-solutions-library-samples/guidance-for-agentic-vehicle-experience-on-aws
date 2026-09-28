# AVX core: cross-system access

What AVX core (spec `2026-09-18-avx-tier2-ios-cards-and-actions`) needs from ADP, CMS and DMS,
how each path is authorized, and how to check it. Staging runs all four systems in one AWS
account, so today most of these paths are same-account. Each section says what would change
in a split-account deployment.

Source of truth is the CDK and the handlers. Every claim below names the file it comes from.

## 1. CVX Tier 2 → ADP (Athena over the ADP lake)

The Tier 2 health agent (`vsa-{stage}-tier2-health-agent-{region}`, role
`vsa-{stage}-tier2-scheduler-{region}`) reads three ADP products: `energy_usage` (battery),
`tire_health` (tires), `service_records` (brakes).

**IAM on the Tier 2 role** (`infrastructure/lib/vsa-tier2-stack.ts`):

| Sid | Grants |
|---|---|
| `AthenaWorkgroupAccess` | `athena:StartQueryExecution`, `GetQueryExecution`, `GetQueryResults` on workgroup `cvx-{stage}-analytics` (ADP region) |
| `AthenaResultsBucketAccess` | read/write on the results bucket `cvx-{stage}-athena-results-{account}-us-east-1` |
| `AdpLakeS3Read` / `AdpLakeS3List` | `s3:GetObject` on `adp-{stage}-foundation-lake-{adpAccount}-{adpRegion}/curated/{energy_usage,tire_health,service_records}/*`; list limited to those prefixes |
| `AdpLakeKmsDecrypt` | `kms:Decrypt`, `kms:DescribeKey` on ADP-account keys, conditioned on `kms:ViaService = s3.{adpRegion}.amazonaws.com` |
| `AdpGlueMetadataRead` | `glue:GetDatabase`, `GetTable`, `GetTables`, `GetPartitions` on the catalog and the three databases and their tables |
| `LakeFormationGetDataAccess` | `lakeformation:GetDataAccess` on `*` (LF has no resource ARN; the LF grants below decide what is readable) |

**Lake Formation grants** (`infrastructure/lib/vsa-analytics-stack.ts`, stack
`vsa-{stage}-analytics`, deployed in the ADP region because `CfnPrincipalPermissions` is
regional): `DESCRIBE` on each database and `SELECT, DESCRIBE` on all tables of
`adp_{stage}_{energy_usage,tire_health,service_records}`, for the Tier 2 role.

**Split account**: the IAM statements are already parameterised on `adpAccount` / `adpRegion`.
The LF grants would move to the ADP account, as a cross-account grant to the CVX role.

**Check**:

```bash
aws lakeformation list-permissions --region us-east-1 --principal \
  "DataLakePrincipalIdentifier=arn:aws:iam::<account>:role/vsa-staging-tier2-scheduler-us-west-2" \
  --query 'PrincipalResourcePermissions[].[Resource.Database.Name || Resource.Table.DatabaseName, join(`,`, Permissions)]' \
  --output text
```

Expect `DESCRIBE` on the three databases and `SELECT,DESCRIBE` on their tables.
`scripts/live-verify-fg15-owner-projection.py --parts B` checks the deployed Tier 2 package and
its CMS grants. Group 10 Steps 1–2 (`verification-run.md`) are the end-to-end proof that these
reads work.

## 2. CVX Tier 2 → CMS vehicle registry (standing projection)

The Tier 2 writer projects `owner_id` / `fleet_id` / `vehicle_id` onto VIN-scoped Findings from
`cms-{stage}-storage-vehicles` (`agents/tier2/owner_resolver.py`).

**IAM** (`vsa-tier2-stack.ts`):
- `CmsVehiclesVinIndexQuery`: `dynamodb:Query` on `…/index/vin-index` only.
- `CmsVehiclesOwnerGetItem`: `dynamodb:GetItem` on the table.

**Split account**: it would need a resource policy on the CMS table, or a role in the CMS account.

**Check**: `LIVE_TESTS=1 .venv/bin/python scripts/live-verify-fg15-owner-projection.py --parts B`
asserts both grants are scoped exactly this way on the deployed role.

## 3. CVX approve → DMS repair orders (no IAM; the caller's token)

`POST /findings/{finding_id}/approve` with `target_system=dms`
(`lambdas/api-avx-findings/handler.py`) dispatches through `agents/executors/registry.py` to
`DmsExecutor` (`agents/executors/dms.py`). The executor calls
`POST ${VSA_DMS_API_ENDPOINT}/api/dms/fleet/repair-orders` over HTTPS, **forwarding the caller's
own Cognito ID token**. It never uses the Lambda's IAM role: a role would be a confused deputy.
So no IAM grant exists between the two systems. Authorization happens twice, once on each side:

| Gate | Requires |
|---|---|
| CVX API authorizer | a token from a CVX tenant pool or the CMS pool |
| CVX approve handler | standing over the Finding, or `service-advisor` / `platform-admin`; for `dms`, also `fleet-operator` or `platform-admin` |
| DMS API authorizer | a CMS-pool token (`CmsPoolAuthorizer`) |
| DMS create-RO | `fleet-operator` (fleet scope on the VIN), or `platform-admin` |

- **Endpoint**: SSM `/cvx/{stage}/dms-api-endpoint` → Lambda env `VSA_DMS_API_ENDPOINT`
  (`infrastructure/lib/vsa-api-stack.ts`).
- **Request body**: `{vehicle_vin, dealer_id, avx_action_id}`. The VIN comes from the Finding.
  `avx_action_id` is the Action id's UUID without the `AVX-ACT-` prefix. DMS deduplicates AVX
  bookings on it, which is what makes the executor's retries safe. DMS commits `e2085e1`,
  `216a8d6`.
- **IAM the findings Lambda does hold for this path**, and only for the D5 failure escalation:
  - `AvxExecutorEscalationPutEvents`: `events:PutEvents` on the account's `default` bus.
  - `AvxExecutorEscalationPutMetric`: `cloudwatch:PutMetricData`, conditioned on namespace
    `AVX/Executor`.

**Split account**: nothing IAM-side changes. Only the endpoint and the pool trust are involved.

**Check**:
- `LIVE_TESTS=1 .venv/bin/python scripts/live-verify-dms-booking-idempotency.py` expects 201,
  then 200 with the same `ro_id`, then 409.
- `LIVE_TESTS=1 .venv/bin/python scripts/live-verify-g10-steps-4-6.py` runs the full approve path.

**Deploy order**: DMS before CVX. See `HANDOFF-g10-ready.md` § "DEPLOY-ORDER GATE" in the spec
dir.

## 4. DMS → CVX: repair-order status events

DMS publishes `dms.ro.status_changed` to its bus `dms-{stage}-events`. CVX owns a rule on that
bus (`vsa-tier2-stack.ts`, `AvxRoStatusChangedRule`, `vsa-{stage}-avx-ro-status-changed`):

- **Pattern**: `source: dms.service-lane`, `detail-type: dms.ro.status_changed`,
  `account: <dmsAccountId>`.
- **Target**: `vsa-{stage}-avx-dms-event-consumer-{region}`. It projects `Draft → in_progress`
  and `Ready`/`Delivered → completed` onto the Action, with grant `AvxActionsProjection`
  (conditional `UpdateItem` on the actions table).

`dmsAccountId` is CDK context. It defaults to the CVX account, and synth warns when it does.

**Known defect (open)**: the consumer looks up `action_id = detail.avx_action_id` verbatim.
DMS carries the bare UUID, so the lookup never matches, and every event is a
`no_op_conditional_check_failed`. This was seen live on 2026-09-24
(`verification-run-steps-4-6.md`). The fix, re-prefixing `AVX-ACT-`, belongs to spec
`2026-09-18-dms-avx-agentic-surface` (T5.4/T5.5). Until it lands, Actions stop at the
`in_progress` the approve handler recorded.

**Split account**: an EventBridge rule must live in the same account as its bus. A split-account
deployment needs a DMS-side rule forwarding to a CVX bus, plus a resource policy on the CVX
bus. This stack does not provide that design.

## 5. DMS UI → CVX Findings/Actions API (no IAM; the user's token)

The DMS frontend calls the CVX REST API directly (`frontend/src/api/dms-client.ts`
`cvxRequest`, in the DMS repo) with the signed-in user's CMS-pool token. The CVX authorizer
trusts the CMS pool, and the Findings/Actions handlers apply their scope rules. Nothing reads
the Findings table cross-account.

**Known defect (open)**: DMS `approveFinding()` posts an empty body. CVX answers
`400 target_system required`. This is DMS-side.

## Related

- API contract: `docs/api/avx-findings-actions.openapi.yaml` (v2.3; pins in
  `tests/schemas/test_contract_fixtures_byte_identical.py`).
- Identity and standing: `docs/identity-and-standing-contract.md`.
- Live verification: exercise the read routes end-to-end after applying the
  grants and confirm entries appear in the target account's CloudTrail data
  events. See `docs/DEPLOYMENT.md` § "Smoke tests" for the invocation pattern.
