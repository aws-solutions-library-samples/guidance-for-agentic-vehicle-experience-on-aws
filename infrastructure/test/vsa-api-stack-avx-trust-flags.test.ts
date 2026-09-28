/**
 * AVX claim-trust attestation wiring.
 *
 * `lambdas/_lib/avx_scope.py` authorizes each standing only when the matching env var
 * is exactly "true". Setting it is the operator's attestation required by
 * `docs/identity-and-standing-contract.md` § 5.1. The wiring must keep these properties:
 *
 *   1. Without a literal, each flag resolves from its SSM parameter at deploy time.
 *      An unseeded parameter fails the deploy rather than defaulting.
 *   2. Fleet-ids trust reaches BOTH the findings Lambda AND the actions Lambda. Customer-id
 *      and vehicle-id trust reach ONLY the findings Lambda (the actions Lambda reads only
 *      AVX_FLEET_IDS_CLAIM_TRUSTED). No other function receives any trust variable.
 *   3. Omitting a prop leaves the variable unset (the untrusted default).
 *   4. `bin/vsa.ts` always supplies all three, each under a distinct synth override name
 *      that differs from the Lambda's own variable name. Unit tests set the Lambda names
 *      freely, and a shell that ran them must not carry a trust decision into a deploy.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as fs from 'fs';
import * as path from 'path';
import { ConfigValue } from '../lib/config-value';
import { VsaApiStack } from '../lib/vsa-api-stack';

const VAR = 'AVX_FLEET_IDS_CLAIM_TRUSTED';
const VAR_CUSTOMER = 'AVX_CUSTOMER_ID_CLAIM_TRUSTED';
const VAR_VEHICLE  = 'AVX_VEHICLE_ID_CLAIM_TRUSTED';
const TRUST_VARS = [VAR, VAR_CUSTOMER, VAR_VEHICLE];
const OTHER_TRUST_VARS = [
  'AVX_DEALER_IDS_CLAIM_TRUSTED',
  'AVX_DISTRICT_IDS_CLAIM_TRUSTED',
];
const FINDINGS_FN = 'vsa-staging-api-avx-findings';
const ACTIONS_FN  = 'vsa-staging-api-avx-actions';

let stackCount = 0;

interface TrustProps {
  fleetTrust?: ConfigValue;
  customerTrust?: ConfigValue;
  vehicleTrust?: ConfigValue;
}

function makeTemplate({ fleetTrust, customerTrust, vehicleTrust }: TrustProps = {}): Template {
  const id = ++stackCount;
  const stage = 'staging';
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };
  const parent = new cdk.Stack(app, `TrustParent${id}`, { env });
  const tbl = (n: string) => dynamodb.Table.fromTableName(parent, `T${id}-${n}`, n);
  const fn = (n: string, arn: string) => lambda.Function.fromFunctionArn(parent, `F${id}-${n}`, arn);

  const stack = new VsaApiStack(app, `vsa-${stage}-api-trust-${id}`, {
    env,
    stage,
    cmsStage: stage,
    tenantConfigTable: tbl(`cms-${stage}-tenant-config`),
    classifier: fn('C', `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-triage-classifier`),
    cmsKmsKeyArn: { literal: 'arn:aws:kms:us-west-2:123456789012:key/test-key', parameterName: `/cvx/${stage}/cms-kms-key-arn` },
    redisEndpoint: { literal: 'redis://stub.redis.local:6379', parameterName: `/cvx/${stage}/redis-endpoint` },
    connectInstanceArn: { literal: 'arn:aws:connect:us-east-1:123456789012:instance/test-instance', parameterName: `/cvx/${stage}/connect-instance-arn` },
    dmsApiEndpoint: { literal: 'https://dms.example.invalid/staging', parameterName: `/cvx/${stage}/dms-api-endpoint` },
    ...(fleetTrust    ? { avxFleetIdsClaimTrusted:    fleetTrust    } : {}),
    ...(customerTrust ? { avxCustomerIdClaimTrusted:  customerTrust } : {}),
    ...(vehicleTrust  ? { avxVehicleIdClaimTrusted:   vehicleTrust  } : {}),
    vpcId: 'vpc-00000000000000000',
    redisSecurityGroupId: 'sg-00000000000000000',
    assistantChatRuntimeArn: {
      literal: `arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/stub-${stage}`,
      parameterName: `/cvx/${stage}/assistant-chat-runtime-arn`,
    },
    assistantChatRuntimeRegion: 'us-west-2',
    allowedOrigin: { literal: 'https://stub.example.com', parameterName: `/cvx/${stage}/allowed-origin` },
    acquireReservationHandoffFn: fn('H', `arn:aws:lambda:us-west-2:123456789012:function:vsa-${stage}-acquire-handoff`),
    acquireReservationHandoffFnArnExportName: `vsa-${stage}-acquire-reservation-handoff-fn-arn`,
    acquireCatalogTable: tbl(`vsa-${stage}-acquire-catalog`),
    acquireOrdersTable: tbl(`vsa-${stage}-acquire-orders`),
    acquireLeadsTable: tbl(`vsa-${stage}-acquire-leads`),
    avxFindingsTableNameExportName: `vsa-${stage}-avx-findings-table-name`,
    avxActionsTableNameExportName: `vsa-${stage}-avx-actions-table-name`,
    avxFeedbackTableNameExportName: `vsa-${stage}-avx-feedback-table-name`,
    notificationDevicesTableNameExportName: `vsa-${stage}-notification-devices-table-name`,
    rsaDispatchTable: dynamodb.Table.fromTableArn(
      parent, `R${id}`, `arn:aws:dynamodb:us-west-2:123456789012:table/vsa-${stage}-rsa-dispatch`,
    ),
    rsaDispatchKey: kms.Key.fromKeyArn(parent, `RK${id}`, 'arn:aws:kms:us-west-2:123456789012:key/test-rsa-dispatch-key'),
    rsaPositionMaxAgeSeconds: { literal: '900', parameterName: `/cvx/${stage}/rsa-position-max-age-seconds` },
  });
  return Template.fromStack(stack);
}

/** FunctionName -> environment variables, for every function in the template. */
function envByFunction(t: Template): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const [logicalId, r] of Object.entries(t.findResources('AWS::Lambda::Function')) as [string, any][]) {
    const name = typeof r.Properties.FunctionName === 'string' ? r.Properties.FunctionName : logicalId;
    out[name] = r.Properties.Environment?.Variables ?? {};
  }
  return out;
}

describe('AVX claim-trust attestation wiring', () => {
  test('1. unset literals resolve from the stage SSM parameters at deploy time', () => {
    const t = makeTemplate({
      fleetTrust:    { parameterName: '/cvx/staging/avx-fleet-ids-claim-trusted' },
      customerTrust: { parameterName: '/cvx/staging/avx-customer-id-claim-trusted' },
      vehicleTrust:  { parameterName: '/cvx/staging/avx-vehicle-id-claim-trusted' },
    });
    const envs = envByFunction(t);
    const params = t.toJSON().Parameters ?? {};

    // Per-function set of vars that must resolve from SSM when passed as parameter-only.
    // api-avx-findings reads all three; api-avx-actions reads only AVX_FLEET_IDS_CLAIM_TRUSTED.
    // See test 2 for the exact-allowlist rationale.
    const checksPerFn: Array<[string, string, string]> = [
      [FINDINGS_FN, VAR,          'avx-fleet-ids-claim-trusted'],
      [FINDINGS_FN, VAR_CUSTOMER, 'avx-customer-id-claim-trusted'],
      [FINDINGS_FN, VAR_VEHICLE,  'avx-vehicle-id-claim-trusted'],
      [ACTIONS_FN,  VAR,          'avx-fleet-ids-claim-trusted'],
      // AVX_CUSTOMER_ID_CLAIM_TRUSTED and AVX_VEHICLE_ID_CLAIM_TRUSTED are NOT wired
      // to the actions Lambda (handler.py does not read them — see test 2).
    ];

    for (const [fn, tvar, kebab] of checksPerFn) {
      const value = envs[fn][tvar] as any;
      expect({ fn, tvar, defined: !!value }).toEqual({ fn, tvar, defined: true });
      expect({ fn, tvar, isRef: Object.keys(value ?? {})[0] }).toEqual({ fn, tvar, isRef: 'Ref' });
      const p = params[value.Ref];
      expect({ fn, tvar, type: p?.Type }).toEqual({ fn, tvar, type: 'AWS::SSM::Parameter::Value<String>' });
      expect({ fn, tvar, default: p?.Default }).toEqual({ fn, tvar, default: `/cvx/staging/${kebab}` });
    }
  });

  test('2. fleet-ids trust reaches findings AND actions; customer-id and vehicle-id trust reach findings ONLY; dealer/district absent everywhere', () => {
    const t = makeTemplate({
      fleetTrust:    { literal: 'true', parameterName: '/cvx/staging/avx-fleet-ids-claim-trusted' },
      customerTrust: { literal: 'true', parameterName: '/cvx/staging/avx-customer-id-claim-trusted' },
      vehicleTrust:  { literal: 'true', parameterName: '/cvx/staging/avx-vehicle-id-claim-trusted' },
    });
    const envs = envByFunction(t);

    // ── Exact per-function allowlist ────────────────────────────────────────────
    //
    // api-avx-findings reads AVX_FLEET_IDS_CLAIM_TRUSTED (fleet scope),
    //   AVX_CUSTOMER_ID_CLAIM_TRUSTED (owner standing), and
    //   AVX_VEHICLE_ID_CLAIM_TRUSTED (driver standing — custom:vehicleId).
    //
    // api-avx-actions bundles _lib/avx_scope.py but only actively reads
    //   AVX_FLEET_IDS_CLAIM_TRUSTED, reached via _trusted_claim_list in:
    //   - _patch_action's fleet-operator PATCH gate (api-avx-actions/handler.py)
    //   - is_fleet_vehicle_caller (handler.py:694)
    //   - check_vehicle_scope (handler.py:782)
    //   - check_owner_fleet_scope (handler.py:820)
    //   - check_fleet_scope (handler.py:842 -> _lib/avx_scope.py:406)
    //   ("only" is among the three standing-claim flags; actions also reads the
    //   dealer and district flags, which stay unset everywhere and fail closed.)
    //   AVX_CUSTOMER_ID_CLAIM_TRUSTED and AVX_VEHICLE_ID_CLAIM_TRUSTED are NOT
    //   read by handler.py or any code reachable from it in the actions Lambda.
    //   Wiring unused vars ships inert config that confuses future readers; they
    //   are absent here and absent from vsa-api-stack.ts for api-avx-actions.
    //
    // Any unknown trust var on either function fails this test.
    const EXPECTED_TRUST_VARS: Record<string, string[]> = {
      [FINDINGS_FN]: [VAR, VAR_CUSTOMER, VAR_VEHICLE].sort(),
      [ACTIONS_FN]:  [VAR].sort(),  // only FLEET_IDS used by actions handler
    };

    for (const [fn, expectedVars] of Object.entries(EXPECTED_TRUST_VARS)) {
      const actualVars = Object.keys(envs[fn] ?? {})
        .filter(k => /_CLAIM_TRUSTED$/.test(k))
        .sort();
      expect({ fn, trustVars: actualVars }).toEqual({ fn, trustVars: expectedVars });
    }

    // No other function carries any trust var.
    for (const [fn, env] of Object.entries(envs)) {
      if (fn === FINDINGS_FN || fn === ACTIONS_FN) continue;
      const unwanted = Object.keys(env).filter(k => /_CLAIM_TRUSTED$/.test(k));
      expect({ fn, unwanted }).toEqual({ fn, unwanted: [] });
    }

    // Forbidden flags absent from every function.
    for (const other of OTHER_TRUST_VARS) {
      const withOther = Object.entries(envs).filter(([, e]) => other in e).map(([n]) => n);
      expect({ other, withOther }).toEqual({ other, withOther: [] });
    }

    // Non-vacuity: the walk saw more than just those two functions.
    expect(Object.keys(envs).length).toBeGreaterThan(5);
  });

  test('3. omitting all three props leaves the variables unset (untrusted default)', () => {
    const envs = envByFunction(makeTemplate({}));
    for (const fn of [FINDINGS_FN, ACTIONS_FN]) {
      for (const tvar of TRUST_VARS) {
        expect({ fn, tvar, present: tvar in (envs[fn] ?? {}) }).toEqual({ fn, tvar, present: false });
      }
    }
  });

  test('4. bin/vsa.ts declares all three with distinct synth override names and passes them to VsaApiStack', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'bin', 'vsa.ts'), 'utf-8');

    const checks = [
      { prop: 'avxFleetIdsClaimTrusted',   envVar: 'CVX_AVX_FLEET_IDS_CLAIM_TRUSTED',   kebab: 'avx-fleet-ids-claim-trusted' },
      { prop: 'avxCustomerIdClaimTrusted',  envVar: 'CVX_AVX_CUSTOMER_ID_CLAIM_TRUSTED',  kebab: 'avx-customer-id-claim-trusted' },
      { prop: 'avxVehicleIdClaimTrusted',   envVar: 'CVX_AVX_VEHICLE_ID_CLAIM_TRUSTED',   kebab: 'avx-vehicle-id-claim-trusted' },
    ];

    const callBlock = src.match(/new VsaApiStack\(app,[\s\S]*?\n\}\);/);
    expect(callBlock).not.toBeNull();

    for (const { prop, envVar, kebab } of checks) {
      const re = new RegExp(`const\\s+${prop}\\s*=\\s*ssmBacked\\(\\{([\\s\\S]*?)\\}\\);`);
      const decl = src.match(re);
      expect({ prop, found: !!decl }).toEqual({ prop, found: true });
      const body = decl![1];
      expect({ prop, envVar: body.includes(envVar) }).toEqual({ prop, envVar: true });
      expect({ prop, kebab: body.includes(kebab)   }).toEqual({ prop, kebab: true });
      expect({ prop, ctxKey: body.includes('contextKey') }).toEqual({ prop, ctxKey: false });
      expect({ prop, inCall: callBlock![0].includes(prop) }).toEqual({ prop, inCall: true });
    }
  });
});
