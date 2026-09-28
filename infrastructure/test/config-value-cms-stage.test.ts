/**
 * `resolveCmsStage` — cross-stage coherence guard.
 *
 * Regression cover for
 * `issues/2026-09-21-cms-stage-env-var-overrides-stage-default/`: an ambient
 * `CMS_STAGE=prod` from a stale, gitignored repo-root `.env` outranked
 * `STAGE_DEFAULTS.staging.cmsStage`, so a `vsa-staging-api` deploy proposed 14
 * IAM statement changes repointing every CMS table grant at `cms-prod-*` —
 * including write and delete verbs on `cms-prod-storage-service-history` and
 * `cms-prod-vfo-action-queue`.
 *
 * These assert the PROPERTY (a contradiction is refused; a coherent pairing is
 * honoured), not the presence of a guard. Each was mutation-verified on
 * 2026-09-21 — see `decisions` note in the issue's `summary.md`.
 */

import { resolveCmsStage } from '../lib/config-value';

describe('resolveCmsStage — the 2026-09-21 defect', () => {
  it('REFUSES the exact observed case: staging stage with an ambient CMS_STAGE=prod', () => {
    expect(() =>
      resolveCmsStage({ stage: 'staging', envValue: 'prod', stageDefault: 'staging' }),
    ).toThrow(/contradicts the canonical CMS stage/);
  });

  it('names both values, both remedies, and the stale-.env cause in the message', () => {
    let message = '';
    try {
      resolveCmsStage({ stage: 'staging', envValue: 'prod', stageDefault: 'staging' });
    } catch (e) {
      message = (e as Error).message;
    }
    // The operator has to be able to act on this without reading the source.
    expect(message).toContain('CMS_STAGE="prod"');
    expect(message).toContain('VSA_STAGE="staging"');
    expect(message).toContain('cms-prod-*');
    expect(message).toContain('CMS_STAGE=staging');
    expect(message).toContain('.env');
    // And it must say what proceeding would actually cost, not just that it refused.
    expect(message).toMatch(/write and delete/);
  });

  it('REFUSES the mirror case too — prod stage with an ambient CMS_STAGE=staging', () => {
    // Guards the direction that would point prod at staging data. Symmetry is
    // the property; a one-sided check would pass while half the hazard remained.
    expect(() =>
      resolveCmsStage({ stage: 'prod', envValue: 'staging', stageDefault: 'prod' }),
    ).toThrow(/contradicts the canonical CMS stage/);
  });
});

describe('resolveCmsStage — states that must keep working', () => {
  it('honours an env var that AGREES with the stage default', () => {
    expect(
      resolveCmsStage({ stage: 'staging', envValue: 'staging', stageDefault: 'staging' }),
    ).toBe('staging');
  });

  it('falls back to the stage default when the env var is unset', () => {
    expect(
      resolveCmsStage({ stage: 'staging', envValue: undefined, stageDefault: 'staging' }),
    ).toBe('staging');
  });

  it('honours the env var for a stage with NO canonical default', () => {
    // A scratch/dev stage absent from STAGE_DEFAULTS has nothing to contradict,
    // and the environment is the only source available. Rejecting here would
    // break the one case the variable legitimately serves.
    expect(
      resolveCmsStage({ stage: 'scratch', envValue: 'dev', stageDefault: undefined }),
    ).toBe('dev');
  });

  it("falls back to 'dev' when neither is supplied", () => {
    expect(
      resolveCmsStage({ stage: 'scratch', envValue: undefined, stageDefault: undefined }),
    ).toBe('dev');
  });

  it.each(['', '   ', '\t\n'])(
    'treats a blank env value (%j) as UNSET, not as a mismatch',
    (blank) => {
      // `CMS_STAGE=` in a sourced file is an operator clearing the value. Reading
      // it as a value would turn a deliberate clear into a spurious hard failure.
      expect(
        resolveCmsStage({ stage: 'staging', envValue: blank, stageDefault: 'staging' }),
      ).toBe('staging');
    },
  );

  it('trims surrounding whitespace before comparing, so " staging " is not a mismatch', () => {
    expect(
      resolveCmsStage({ stage: 'staging', envValue: ' staging ', stageDefault: 'staging' }),
    ).toBe('staging');
  });
});
