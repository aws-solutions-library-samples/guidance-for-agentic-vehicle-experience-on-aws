import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cognitoIdentity from 'aws-cdk-lib/aws-cognito-identitypool';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * Kiosk group name — a published contract.
 *
 * `lambdas/api-assistant-chat/persona_map.py` matches on this exact string
 * to clamp the persona to `consumer` for Zone-2 tablet traffic.
 * Do NOT rename; doing so silently breaks persona clamping without a test
 * failure because Python string comparison is case-sensitive.
 */
export const KIOSK_GROUP_NAME = 'kiosk';

export interface TenantPoolProps {
  readonly stage: string;
  readonly tenantId: string;  // e.g. 'meridian-fleet'
  readonly displayName: string;

  /**
   * Optional AgentCore runtime ARN pattern that authenticated users of this
   * tenant should be allowed to invoke. When provided, a Cognito Identity
   * Pool + authenticated IAM role are created with an inline policy granting
   * `bedrock-agentcore:InvokeAgentRuntime` on that pattern. When absent, no
   * identity pool is created and the construct behaves as a plain user pool.
   *
   * Wildcards are allowed — e.g. `arn:aws:bedrock-agentcore:us-east-1:123:runtime/vsa_supervisor_bidi-*`
   * to cover redeploys that change the runtime's ARN suffix.
   *
   * The Swift iOS client exchanges the user pool IdToken for AWS temp
   * credentials via `GetCredentialsForIdentity` against this pool, then
   * uses those credentials to SigV4-sign the WebSocket handshake to
   * AgentCore. Without this, the client can authenticate the user but
   * cannot invoke AgentCore directly.
   */
  readonly voiceRuntimeArnPattern?: string;

  /**
   * Additional externally-owned Cognito user pools whose authenticated
   * tokens should ALSO be trusted by this tenant's voice identity pool for
   * the AgentCore SigV4 credential exchange (`GetCredentialsForIdentity`).
   * Each entry needs both the pool id and an app client id; both are
   * imported by id, so CDK never mutates or recreates them.
   *
   * Use case: the iOS client now signs users in against the CONSOLIDATED
   * CMS UI user pool (one pool per deployment serves Fleet UI + iOS), not
   * the per-tenant CVX pool created above. Without trusting that pool here,
   * the identity pool rejects CMS-pool tokens with NotAuthorizedException
   * "Token is not from a supported provider of this identity pool" — see
   * issues/2026-06-19-cvx-voice-identity-pool-provider-mismatch.
   *
   * Only consulted when `voiceRuntimeArnPattern` is also set (i.e. an
   * identity pool actually exists). The per-tenant CVX pool always remains
   * a trusted provider; entries here are additive.
   */
  readonly additionalVoiceAuthUserPools?: ReadonlyArray<{
    readonly userPoolId: string;
    readonly userPoolClientId: string;
  }>;

  /**
   * When `true`, provisions the Zone-2 kiosk identity surface:
   *   - A Cognito group named exactly `kiosk` (see `KIOSK_GROUP_NAME`).
   *   - A least-privilege IAM role bound to that group (allows
   *     `execute-api:Invoke` scoped to `vsaApiId` if provided, or
   *     falls back to `arn:aws:execute-api:*:*:*` if not).
   *   - A dedicated kiosk app client with shortened token validity:
   *     access/ID tokens 5 minutes, refresh 8 hours (one show shift).
   *
   * Outputs added when enabled:
   *   - `KioskClientId`   — app client id for device provisioning
   *   - `KioskGroupName`  — always `kiosk`; exported for runbook scripts
   *
   * Security invariants:
   *   - No kiosk USERS are created in CDK. User creation is an operator
   *     provisioning step (see docs/DEPLOYMENT.md § Kiosk device setup).
   *     Credentials in a CloudFormation template are a git-commit leak; the
   *     only remediation is rotation (secrets-handling.md).
   *   - `allowUnauthenticatedIdentities` on the voice identity pool is NOT
   *     changed. D2 preserves the platform-wide `false` posture; kiosk
   *     devices are authenticated (named) principals, not anonymous ones.
   *
   * @default false
   */
  readonly enableKioskGroup?: boolean;

  /**
   * REST API Gateway id (e.g. `abc1234def`) of the VSA API that kiosk
   * devices are permitted to invoke.  Required when `enableKioskGroup` is
   * `true` to produce a properly-scoped `execute-api:Invoke` resource ARN:
   *
   *   `arn:aws:execute-api:<region>:<account>:<apiId>/*`
   *
   * When omitted the role falls back to `arn:aws:execute-api:*:*:*`, which
   * is intentionally broad and is only acceptable if the API id is genuinely
   * unavailable at construct instantiation time.
   *
   * Security rationale (decisions.md Q14 / security-review Cycle 6 W2):
   * the kiosk credential lives on a tablet the public physically handles.
   * A wildcard `execute-api` grant means a stolen refresh token can call
   * any API Gateway in any region/account the role can reach — a much
   * larger surface than the six `/acquire/*` routes the kiosk needs.
   * Threading the API id here reduces that surface to the single deployed
   * REST API.
   */
  readonly vsaApiId?: string;
}

/** Per-tenant Cognito User Pool + App Client. Admin-created users only.
 *  Pool name: vsa-{stage}-{tenantId}.
 *  Users carry a custom:tenantId attribute for API authorization checks.
 *
 *  If `voiceRuntimeArnPattern` is provided, also creates a Cognito Identity
 *  Pool + authenticated role for direct AgentCore WebSocket access from the
 *  iOS client. See the prop docstring for the auth flow.
 *
 *  If `enableKioskGroup` is true, also creates the Zone-2 kiosk identity
 *  surface: group named `kiosk`, scoped IAM role, dedicated app client with
 *  shortened token validity. See the prop docstring for the security rationale.
 */
export class TenantPool extends Construct {
  readonly userPool: cognito.UserPool;
  readonly userPoolClient: cognito.UserPoolClient;

  /** Populated only when `voiceRuntimeArnPattern` was provided. */
  readonly identityPool?: cognitoIdentity.IdentityPool;
  /** Authenticated role bound to `identityPool`. Holds voice-invoke grants. */
  readonly authenticatedRole?: iam.Role;

  /**
   * Dedicated kiosk app client with shortened token validity.
   * Populated only when `enableKioskGroup` is `true`.
   * Operators use this client id (NOT `userPoolClient`) when provisioning
   * Zone-2 tablet credentials so the token lifetime is bounded.
   */
  readonly kioskClient?: cognito.UserPoolClient;

  /**
   * IAM role bound to the `kiosk` Cognito group.
   * Populated only when `enableKioskGroup` is `true`.
   * Included in the `cognito:preferred_role` claim for kiosk tokens.
   * Scoped to `execute-api:Invoke` on `*`; expands to specific API IDs
   * in a future hardening pass once the API Gateway ID is a stable output.
   */
  readonly kioskRole?: iam.Role;

  constructor(scope: Construct, id: string, props: TenantPoolProps) {
    super(scope, id);

    this.userPool = new cognito.UserPool(this, 'Pool', {
      userPoolName: `vsa-${props.stage}-${props.tenantId}`,
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: false },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8, requireLowercase: true, requireUppercase: true,
        requireDigits: true, requireSymbols: true,
      },
      customAttributes: {
        tenantId: new cognito.StringAttribute({ mutable: false }),
        role: new cognito.StringAttribute({ mutable: true }),
        // These two are present on the live Meridian Fleet pool from a prior CDK
        // revision. They must stay declared here because Cognito forbids
        // removal of schema attributes — a CDK-driven delete triggers a
        // full-stack rollback, which cascade-deletes every dependent
        // Lambda/route. Learned this the painful way on 2026-04-30.
        driverId: new cognito.StringAttribute({ mutable: false }),
        vehicleId: new cognito.StringAttribute({ mutable: true }),
      },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // WriteAttributes is set explicitly to the IdP-mapped minimum {email, name}.
    //
    // Leaving WriteAttributes unset (the previous state of this client) is the
    // vulnerable default: it permits a self-registered or admin-created user to
    // overwrite ANY attribute — including custom ones — through UpdateUserAttributes
    // or SignUp, bypassing admin-only provisioning. Verified live on the CMS pool
    // 2026-08-10 (see connected-mobility/deployment/stacks/test_client_write_attributes.py).
    //
    // The two standard attributes kept writable are required for IdP-mapped sign-in
    // (the iOS Federate flow maps email → EMAIL and name → GIVEN_NAME; removing write
    // access to a mapped attribute causes Cognito to throw on IdP sign-in).
    //
    // No custom attribute (custom:tenantId, custom:role, custom:driverId,
    // custom:vehicleId) may be client-writable. Those attributes are authorization
    // inputs written exclusively by admin tooling; a client that can write them can
    // escalate its own standing claims. This restriction is a prerequisite for
    // vehicle-id claim trust (see docs/identity-and-standing-contract.md § 5.1).
    const standardWriteAttrs = new cognito.ClientAttributes()
      .withStandardAttributes({ email: true, fullname: true });

    this.userPoolClient = this.userPool.addClient('AppClient', {
      userPoolClientName: `vsa-${props.stage}-${props.tenantId}-app`,
      generateSecret: false,
      authFlows: { userSrp: true, userPassword: true },
      preventUserExistenceErrors: true,
      writeAttributes: standardWriteAttrs,
    });

    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      exportName: `vsa-${props.stage}-${props.tenantId}-pool-id`,
    });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      exportName: `vsa-${props.stage}-${props.tenantId}-client-id`,
    });

    // --- Optional: identity pool + authenticated role for voice -------------
    if (props.voiceRuntimeArnPattern) {
      // AllowUnauthenticatedIdentities=false: every voice session must be
      // tied to a signed-in Cognito user. No anonymous AgentCore invokes.
      //
      // The per-tenant CVX user pool is always a trusted provider. Any
      // externally-owned pools (e.g. the consolidated CMS UI pool the iOS
      // client actually signs into) are appended as additional providers so
      // their tokens are accepted by GetCredentialsForIdentity. Imported by
      // id — CDK only reads their provider name + client id, never mutates.
      const voiceAuthProviders: cognitoIdentity.IUserPoolAuthenticationProvider[] = [
        new cognitoIdentity.UserPoolAuthenticationProvider({
          userPool: this.userPool,
          userPoolClient: this.userPoolClient,
        }),
      ];
      (props.additionalVoiceAuthUserPools ?? []).forEach((ext, i) => {
        const extPool = cognito.UserPool.fromUserPoolId(
          this, `VoiceAuthExtraPool${i}`, ext.userPoolId);
        const extClient = cognito.UserPoolClient.fromUserPoolClientId(
          this, `VoiceAuthExtraClient${i}`, ext.userPoolClientId);
        voiceAuthProviders.push(new cognitoIdentity.UserPoolAuthenticationProvider({
          userPool: extPool,
          userPoolClient: extClient,
        }));
      });

      this.identityPool = new cognitoIdentity.IdentityPool(this, 'IdentityPool', {
        identityPoolName: `vsa-${props.stage}-${props.tenantId}-identity`,
        allowUnauthenticatedIdentities: false,
        authenticationProviders: {
          userPools: voiceAuthProviders,
        },
      });

      // The IdentityPool L2 construct auto-creates a default authenticated
      // role (and a removed-for-safety unauth role since we disabled unauth).
      // We attach the voice invoke policy directly to the auth role.
      this.authenticatedRole = this.identityPool.authenticatedRole as iam.Role;
      this.authenticatedRole.addToPrincipalPolicy(new iam.PolicyStatement({
        sid: 'InvokeVoiceAgentRuntime',
        effect: iam.Effect.ALLOW,
        actions: [
          // REST path (future-proof — we don't use it yet)
          'bedrock-agentcore:InvokeAgentRuntime',
          // Actual action checked on WebSocket handshake. Discovered
          // empirically: the IAM authz check on `wss://.../runtimes/<arn>/ws`
          // requires this action, even though the SDK service model only
          // lists InvokeAgentRuntime as an operation name.
          'bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream',
        ],
        // Covers both the runtime ARN itself and any sub-resources (endpoints)
        // AgentCore's auth layer may check at handshake time. Wildcard on the
        // runtime name suffix tolerates redeploys that mint new suffixes.
        resources: [
          props.voiceRuntimeArnPattern,
          `${props.voiceRuntimeArnPattern}/*`,
        ],
      }));

      new cdk.CfnOutput(this, 'IdentityPoolId', {
        value: this.identityPool.identityPoolId,
        exportName: `vsa-${props.stage}-${props.tenantId}-identity-pool-id`,
      });
    }

    // --- Optional: kiosk group + scoped IAM role + dedicated client -------
    //
    // Zone-2 "anonymous" kiosk identity model (spec D2):
    //   Each Zone-2 tablet authenticates as its own provisioned Cognito user
    //   in this group. "Anonymous" means unattributed to a person, NOT
    //   unauthenticated. Using a named, revocable principal means an operator
    //   can disable a single misbehaving tablet mid-show without touching
    //   any other session or user.
    //
    // What this block does NOT do:
    //   - Does NOT change allowUnauthenticatedIdentities (kept false per D2).
    //   - Does NOT create kiosk users (operator provisioning step; credentials
    //     in a CFN template are a leak — see secrets-handling.md).
    //
    // Token validity rationale:
    //   Default: access/id 60 min, refresh 30 days.
    //   Kiosk client: access/id 5 min (Cognito minimum), refresh 8 h (one
    //   show-floor shift). Short-lived access tokens bound the window between
    //   a revoked credential and the last-issued token expiring. A per-device
    //   user is revocable immediately via AdminDisableUser / AdminUserGlobalSignOut;
    //   the shortened access-token TTL is a defence-in-depth backstop only.
    //   8-hour refresh covers a single shift changeover without forcing a
    //   re-authentication interruption mid-show; operators rotate credentials
    //   after the show per docs/DEPLOYMENT.md § Kiosk device teardown.
    if (props.enableKioskGroup) {
      // ── IAM role for the kiosk group ──────────────────────────────────
      //
      // Scoped to `execute-api:Invoke` so kiosk devices can call the
      // VSA API Gateway routes. This role is recorded in the Cognito token's
      // `cognito:preferred_role` claim for kiosk users and is the principal
      // whose identity pool credential exchange you would use IF you later
      // added kiosk to an identity pool. Today the kiosk flow is JWT-only
      // (Cognito authorizer on API Gateway reads the token directly), so the
      // role is a claim signal and a least-privilege intent declaration.
      //
      // `execute-api:Invoke` is scoped to this deployment's REST API when
      // `vsaApiId` is supplied (decisions.md Q14 / security-review Cycle 6
      // W2): the kiosk credential lives on a tablet the public physically
      // handles, so a wildcard grant means a stolen refresh token can call
      // any API Gateway in any region/account the role can reach.
      // Scoped ARN: `arn:aws:execute-api:<region>:<account>:<apiId>/*`.
      // See cross-region-namespace.md — IAM roles are account-wide, so the
      // role name is region-suffixed.
      const executeApiResource = props.vsaApiId
        ? `arn:aws:execute-api:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:${props.vsaApiId}/*`
        : 'arn:aws:execute-api:*:*:*';

      this.kioskRole = new iam.Role(this, 'KioskRole', {
        roleName: `vsa-${props.stage}-${props.tenantId}-kiosk-${cdk.Stack.of(this).region}`,
        description: `Zone-2 kiosk role for vsa-${props.stage}-${props.tenantId} tenant. ` +
          `Allows execute-api:Invoke so tablet JWTs (cognito:preferred_role) ` +
          `are scoped to API Gateway only. No data-plane or control-plane actions.`,
        assumedBy: new iam.FederatedPrincipal(
          'cognito-identity.amazonaws.com',
          {
            // Standard Cognito Identity federation condition block.
            // Required for the Cognito Group → IAM Role binding to be usable
            // in a GetCredentialsForIdentity flow (even if we don't use
            // federation today — future-proofing). Without this, the AssumeRole
            // trust policy would be too broad.
            StringEquals: { 'cognito-identity.amazonaws.com:aud': cdk.Stack.of(this).account },
            'ForAnyValue:StringLike': { 'cognito-identity.amazonaws.com:amr': 'authenticated' },
          },
          'sts:AssumeRoleWithWebIdentity',
        ),
        inlinePolicies: {
          KioskApiAccess: new iam.PolicyDocument({
            statements: [
              new iam.PolicyStatement({
                sid: 'InvokeVsaApiGateway',
                effect: iam.Effect.ALLOW,
                actions: ['execute-api:Invoke'],
                // Scoped to this deployment's REST API when vsaApiId is
                // provided (security-review Cycle 6 W2). Falls back to
                // `*:*:*` only when the API id is unavailable — which should
                // not happen because VsaApiStack threads it in via this prop.
                resources: [executeApiResource],
              }),
            ],
          }),
        },
      });

      // ── Cognito group — name is a published contract ───────────────────
      //
      // persona_map.py matches `cognito:groups` against the literal string
      // `kiosk` (KIOSK_GROUP_NAME). Renaming this breaks persona clamping.
      this.userPool.addGroup('KioskGroup', {
        groupName: KIOSK_GROUP_NAME,
        description: 'Zone-2 show-floor kiosk tablets. ' +
          'Maps to consumer persona; tool surface is allow-listed server-side.',
        role: this.kioskRole,
        // Lower precedence value = higher priority in multi-group scenarios.
        // 10 keeps kiosk below the floor (0 is highest) so explicit admin or
        // fleet-operator groups still take precedence if a user is ever in both.
        precedence: 10,
      });

      // ── Dedicated kiosk app client with shortened token lifetime ────────
      //
      // Operators use this client id (NOT userPoolClient) when provisioning
      // tablet credentials. Using a dedicated client:
      //   1. Enforces the shortened token TTL without changing the default
      //      client used by every other persona.
      //   2. Provides an audit signal — a token from kioskClient cannot be
      //      confused with a token from the standard client.
      //   3. Allows future hardening (e.g. restricting to device_code flow
      //      or requiring a short auth session validity) without touching
      //      existing clients.
      //
      // Token validity:
      //   - accessTokenValidity: 5 min (Cognito minimum; defence-in-depth
      //     backstop for compromised tokens)
      //   - idTokenValidity:     5 min (same)
      //   - refreshTokenValidity: 8 h  (one show-floor shift; operators
      //     rotate device credentials after show teardown)
      this.kioskClient = this.userPool.addClient('KioskAppClient', {
        userPoolClientName: `vsa-${props.stage}-${props.tenantId}-kiosk`,
        generateSecret: false,
        authFlows: {
          // SRP: devices authenticate with username+password; no admin flow
          // so credentials cannot be used directly against admin APIs.
          userSrp: true,
          userPassword: false,
          adminUserPassword: false,
        },
        preventUserExistenceErrors: true,
        // Shortened token lifetimes — see rationale in block comment above.
        accessTokenValidity: cdk.Duration.minutes(5),
        idTokenValidity: cdk.Duration.minutes(5),
        refreshTokenValidity: cdk.Duration.hours(8),
        // Same write-attribute restriction as the main AppClient: kiosk devices
        // must not be able to overwrite custom authorization attributes.
        // See the standardWriteAttrs comment on AppClient above.
        writeAttributes: standardWriteAttrs,
      });

      // ── Outputs for device provisioning ──────────────────────────────
      //
      // Surfaces the values an operator needs to configure a kiosk device:
      //   UserPoolId  — already emitted above (shared output)
      //   KioskClientId — the dedicated client with shortened validity
      //   KioskGroupName — the exact string operators must add users to
      //
      // NO secrets here: client id is not a credential (the user's password
      // is the secret; secrets-handling.md applies to that, not to the
      // client id). No user passwords, no client secrets (generateSecret=false).
      new cdk.CfnOutput(this, 'KioskClientId', {
        value: this.kioskClient.userPoolClientId,
        description: 'App client id for Zone-2 kiosk device provisioning. ' +
          'Use this client id (not the default app client) to authenticate ' +
          'kiosk tablets - access/ID token validity is 5 min, refresh 8 h.',
        exportName: `vsa-${props.stage}-${props.tenantId}-kiosk-client-id`,
      });

      new cdk.CfnOutput(this, 'KioskGroupName', {
        value: KIOSK_GROUP_NAME,
        description: 'Cognito group name for Zone-2 kiosk devices. ' +
          'Add each provisioned kiosk user to this group so persona_map.py ' +
          'clamps the persona to consumer and applies the tool allow-list.',
        exportName: `vsa-${props.stage}-${props.tenantId}-kiosk-group-name`,
      });
    }
  }
}
