"""Pin the Tier 2 grants and env vars the assigned-driver owner lookup needs.

Issue ``issues/2026-10-07-avx-finding-owner-is-purchaser-not-driver/``. The jest suite
(``infrastructure/test/vsa-tier2-stack-owner-driver.test.ts``) asserts the synthesized template,
but no CI job runs jest, so these properties are also pinned here by reading the CDK
source. Each test names the mutation it catches.
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[3]
_STACK = (_REPO / "infrastructure" / "lib" / "vsa-tier2-stack.ts").read_text()
_BIN = (_REPO / "infrastructure" / "bin" / "vsa.ts").read_text()


def _statement(sid: str) -> str:
    """The text of the ``new iam.PolicyStatement({...})`` carrying ``sid``."""
    m = re.search(r"new iam\.PolicyStatement\(\{\s*sid: '" + re.escape(sid) + r"',(.*?)\}\)\);",
                  _STACK, re.S)
    assert m, f"statement {sid} not found in vsa-tier2-stack.ts"
    return m.group(1)


def _actions(body: str) -> list[str]:
    m = re.search(r"actions:\s*\[([^\]]*)\]", body)
    assert m
    return re.findall(r"'([^']+)'", m.group(1))


def _resources(body: str) -> str:
    m = re.search(r"resources:\s*\[([^\]]*)\]", body)
    assert m
    return m.group(1).strip()


def test_drivers_scan_is_one_action_on_the_drivers_table():
    """Mutations: add an action; resources -> ['*']; point it at storage-vehicles."""
    body = _statement("CmsDriversAssignmentScan")
    assert _actions(body) == ["dynamodb:Scan"]
    assert _resources(body) == "cmsDriversTableArn"
    m = re.search(r"const cmsDriversTableArn =\s*`([^`]+)`", _STACK)
    assert m and m.group(1).endswith(":table/cms-${cmsStage}-storage-drivers")


def test_pool_lookup_is_list_users_on_the_cms_pool_only():
    """Mutations: add AdminGetUser/an admin write; resources -> ['*']; drop the guard."""
    body = _statement("CmsUserPoolDriverLookup")
    assert _actions(body) == ["cognito-idp:ListUsers"]
    assert _resources(body) == (
        "`arn:aws:cognito-idp:${poolRegion}:${cmsAccount}:userpool/${cmsUserPoolId}`"
    )
    # Granted only when the pool id is concrete and well-formed.
    guard = _STACK.index("if (cmsUserPoolId !== undefined) {")
    assert guard < _STACK.index("sid: 'CmsUserPoolDriverLookup'")
    assert re.search(r"const cmsUserPoolId = isResolvedUserPoolId\(props\.cmsUserPoolId\)", _STACK)


def test_no_other_statement_grants_cognito_or_the_drivers_table():
    """A second statement on the same resources would widen what the two above narrow."""
    code = "\n".join(line.split("//", 1)[0] for line in _STACK.splitlines())
    assert code.count("cognito-idp:") == 2  # the action and the ARN in one statement
    assert len(re.findall(r"storage-drivers", code)) == 2  # the ARN and the env var


def test_env_carries_the_drivers_table_and_the_pool():
    assert "CMS_DRIVERS_TABLE:        `cms-${cmsStage}-storage-drivers`," in _STACK
    assert "CMS_USER_POOL_ID:         cmsUserPoolId ?? ''," in _STACK


def test_bin_passes_the_cms_pool_to_tier2():
    """Mutation: drop the prop, and every Finding silently loses its owner."""
    block = _BIN[_BIN.index("new VsaTier2Stack(app"):]
    block = block[: block.index("});")]
    assert "cmsUserPoolId: voiceAuthUserPoolId," in block
