/**
 * Guard: neither app client on TenantPool may let users write custom attributes.
 *
 * ## Why this test exists
 *
 * Leaving WriteAttributes unset is the vulnerable default: a self-registered or
 * admin-created user can call UpdateUserAttributes (or SignUp) to overwrite ANY
 * attribute — including custom:vehicleId, custom:role, custom:driverId — bypassing
 * admin-only provisioning. Verified live on the CMS pool 2026-08-10.
 *
 * For CVX specifically, custom:vehicleId is an authorization input in avx_scope.py:
 * a client that can write it can forge a vehicle-id standing claim. This is the
 * second self-writable pool identified in review Cycle 2 (W6), and restricting
 * WriteAttributes here is the prerequisite for seeding avx-vehicle-id-claim-trusted=true
 * (see docs/identity-and-standing-contract.md § 5.1).
 *
 * The fix is identical to the CMS fix (deployment/stacks/test_client_write_attributes.py):
 * WriteAttributes is set to {email, name} on BOTH the main AppClient and the kiosk
 * client. email and name must remain writable for IdP-mapped sign-in (the Federate/iOS
 * flow maps email → EMAIL and name → GIVEN_NAME; removing write access to a mapped
 * attribute causes Cognito to throw on sign-in).
 *
 * ## Mutation record (required by testing.md)
 *
 * M1 — writeAttributes removed from AppClient (reverts to the unset-default state):
 *   Procedure: remove `writeAttributes: standardWriteAttrs,` from the AppClient addClient call.
 *   Result:
 *     - `AppClient: WriteAttributes is explicitly set` FAILS with "Expected: true / Received: false".
 *     - `AppClient: no custom attribute is client-writable [custom:vehicleId]` FAILS.
 *     - All other tests PASS (correct: kiosk tests are independent).
 *   Restored: yes.
 *
 * M2 — writeAttributes removed from KioskAppClient:
 *   Procedure: remove `writeAttributes: standardWriteAttrs,` from the kioskClient addClient call.
 *   Result:
 *     - `KioskAppClient: WriteAttributes is explicitly set` FAILS.
 *     - `KioskAppClient: no custom attribute is client-writable [custom:vehicleId]` FAILS.
 *     - AppClient tests PASS (correct: independent).
 *   Restored: yes.
 *
 * Both mutations exercised 2026-09-25.
 */

import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { TenantPool } from '../lib/tenant-pool';

// Every custom attribute declared on the pool. None may be client-writable.
const CUSTOM_ATTRS = [
  'custom:tenantId',
  'custom:role',
  'custom:driverId',
  'custom:vehicleId',
];

// Standard attributes that MUST stay writable for IdP-mapped sign-in.
const REQUIRED_WRITABLE = ['email', 'name'];

let stackCount = 0;

function makeTemplate(enableKiosk = false): Template {
  const id = ++stackCount;
  const app = new cdk.App();
  const stack = new cdk.Stack(app, `TestStack${id}`, {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  new TenantPool(stack, 'Pool', {
    stage: 'staging',
    tenantId: 'meridian-fleet',
    displayName: 'Meridian Fleet',
    enableKioskGroup: enableKiosk,
    ...(enableKiosk ? { vsaApiId: 'abc1234def' } : {}),
  });
  return Template.fromStack(stack);
}

/** Return client properties by userPoolClientName. */
function clientsByName(t: Template): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const r of Object.values(t.findResources('AWS::Cognito::UserPoolClient')) as any[]) {
    const p = r.Properties as Record<string, unknown>;
    const name = p.ClientName as string;
    out[name] = p;
  }
  return out;
}

describe('TenantPool app client WriteAttributes guard (T3.4)', () => {
  // ── AppClient (always present) ─────────────────────────────────────────────

  describe('AppClient', () => {
    let clients: Record<string, Record<string, unknown>>;
    beforeAll(() => {
      clients = clientsByName(makeTemplate(false));
    });

    test('AppClient exists in the template', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-app'));
      expect(name).toBeTruthy();
    });

    test('AppClient: WriteAttributes is explicitly set', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-app'))!;
      const props = clients[name];
      expect({
        client: name,
        hasWriteAttributes: 'WriteAttributes' in props,
      }).toEqual({
        client: name,
        hasWriteAttributes: true,
      });
    });

    test.each(CUSTOM_ATTRS)('AppClient: no custom attribute is client-writable [%s]', (attr) => {
      const name = Object.keys(clients).find(n => n.endsWith('-app'))!;
      const writable = (clients[name].WriteAttributes ?? []) as string[];
      expect({ client: name, attr, writable: writable.includes(attr) }).toEqual({
        client: name,
        attr,
        writable: false,
      });
    });

    test.each(REQUIRED_WRITABLE)('AppClient: IdP-mapped attribute remains writable [%s]', (attr) => {
      const name = Object.keys(clients).find(n => n.endsWith('-app'))!;
      const writable = (clients[name].WriteAttributes ?? []) as string[];
      expect({ client: name, attr, writable: writable.includes(attr) }).toEqual({
        client: name,
        attr,
        writable: true,
      });
    });

    test('AppClient: writable set is exactly the minimum {email, name}', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-app'))!;
      const writable = ((clients[name].WriteAttributes ?? []) as string[]).slice().sort();
      expect({ client: name, writable }).toEqual({
        client: name,
        writable: [...REQUIRED_WRITABLE].sort(),
      });
    });
  });

  // ── KioskAppClient (when enableKioskGroup = true) ──────────────────────────

  describe('KioskAppClient', () => {
    let clients: Record<string, Record<string, unknown>>;
    beforeAll(() => {
      clients = clientsByName(makeTemplate(true));
    });

    test('KioskAppClient exists in the template when enableKioskGroup is true', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-kiosk'));
      expect(name).toBeTruthy();
    });

    test('KioskAppClient: WriteAttributes is explicitly set', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-kiosk'))!;
      const props = clients[name];
      expect({
        client: name,
        hasWriteAttributes: 'WriteAttributes' in props,
      }).toEqual({
        client: name,
        hasWriteAttributes: true,
      });
    });

    test.each(CUSTOM_ATTRS)('KioskAppClient: no custom attribute is client-writable [%s]', (attr) => {
      const name = Object.keys(clients).find(n => n.endsWith('-kiosk'))!;
      const writable = (clients[name].WriteAttributes ?? []) as string[];
      expect({ client: name, attr, writable: writable.includes(attr) }).toEqual({
        client: name,
        attr,
        writable: false,
      });
    });

    test.each(REQUIRED_WRITABLE)('KioskAppClient: IdP-mapped attribute remains writable [%s]', (attr) => {
      const name = Object.keys(clients).find(n => n.endsWith('-kiosk'))!;
      const writable = (clients[name].WriteAttributes ?? []) as string[];
      expect({ client: name, attr, writable: writable.includes(attr) }).toEqual({
        client: name,
        attr,
        writable: true,
      });
    });

    test('KioskAppClient: writable set is exactly the minimum {email, name}', () => {
      const name = Object.keys(clients).find(n => n.endsWith('-kiosk'))!;
      const writable = ((clients[name].WriteAttributes ?? []) as string[]).slice().sort();
      expect({ client: name, writable }).toEqual({
        client: name,
        writable: [...REQUIRED_WRITABLE].sort(),
      });
    });
  });
});
