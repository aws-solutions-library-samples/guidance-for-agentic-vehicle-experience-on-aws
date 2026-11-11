"""Fake world for ``scripts/live-verify-fi-loop.py`` (stage D task 9.3 live check).

Extends ``scripts/tests/fi_rw_world`` with the four pieces the loop script needs
that the recall/warranty world does not provide:

1. **AVX investigations route** (``InvestigationsHandler``): the real
   ``lambdas/api-avx-investigations/handler.py`` behind a ``FakeRestApi``-compatible
   shim.  The handler requires its own DynamoDB table and an SQS queue; both are
   created by ``FiLoopWorld``.

2. **fi_quality agent** (``FiQualityBedrock``): a Bedrock Converse fake that follows
   the fi_quality agent's two-call forced-tool contract (``record_root_cause`` then
   ``record_design_options``).  Wired into the shared ``Router`` so the Tier 2 Lambda,
   when invoked with an SQS investigation record, drives the real ``run_investigation``
   against this fake instead of real Bedrock.

3. **ADP writer handler** (``AdpWriterHandler``): the real
   ``platform-foundation/source/lambda/digital_thread_writer/handler.py`` from ADP
   ``9bb82e96``, loaded by path.  Wired as the ``adp-staging-digital-thread-writer``
   Lambda behind the AVX findings handler's ``ADP_THREAD_WRITER_ARN``.  Requires
   several additional DynamoDB tables (digital-thread, recall-campaigns-issued, etc.)
   created by ``FiLoopWorld``.

4. **CMS groundings handler** (``CmsGroundingsHandler``): the real
   ``services/fleet_intelligence/index.py`` handler covering
   ``POST /groundings`` and ``POST /groundings/{vehicleId}/return-to-service``, behind
   a ``FakeRestApi``-compatible shim.  Uses the same moto DynamoDB stack and vehicle
   data as ``fi_rw_world.World``.

Each piece is importable independently for its own test, and ``FiLoopWorld`` wires
them together into a single in-process staging environment.

Memory notes (what is NOT real code and why)
--------------------------------------------
- The fi_quality Bedrock fake is NOT a real Bedrock call.  Real Bedrock would require
  live credentials and would be slow.  The fake follows the forced-tool contract
  exactly: it reads ``toolConfig.toolChoice.tool.name`` and fills the required fields
  from the context text, so ``run_investigation`` + ``validate`` exercise all their
  real code paths.
- The ADP fault-patterns DynamoDB table is stubbed with a minimal synthetic row so the
  writer's ``get_pattern`` / ``get_affected_vins`` calls succeed without a real ADP
  Athena query.
- CMS's recall cache (``lifecycle_cache``) is pre-populated by writing a fake cache
  item directly so the return-to-service route can evaluate blocking recalls without
  running the FI refresh scheduler.
"""

from scripts.tests.fi_loop_world.world import FiLoopWorld

__all__ = ["FiLoopWorld"]
