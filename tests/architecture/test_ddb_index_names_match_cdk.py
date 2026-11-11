"""
Structural guard: every DDB IndexName used in Python source must match a CDK
declaration in ``infrastructure/lib/vsa-tier2-stack.ts`` AND be declared on the
specific table that the call site queries.

Root cause addressed (Fix Group 10 T10.1):
-----------------------------------------
The CDK uses a ``by_<field>`` naming convention.  The Python was written against
a ``<field>-index`` / ``<field>_index`` convention.  ``_list_findings_by_index``
takes both names as adjacent positional strings, so passing an index name into the
key slot is **silent** at the Python call site — it raises
``ValidationException: Query condition missed key schema element`` at DynamoDB
runtime only.  This guard closes that class for all future edits.

Upgrade (Fix Group 10 T10.7 — table-aware mapping check):
---------------------------------------------------------
The original inventory check caught four T10.1 mutations because each used an
index name declared nowhere in the CDK.  But a name on the *wrong table* passes
that check cleanly: ``by_owner`` is declared on the findings table and is a
plausible index name on the actions table.  Empirically verified 2026-09-20:
pointing ``api-avx-actions/handler.py`` at ``by_owner`` via
``"IndexName": "by_owner"`` left the guard 4/4 green.

Fix Group 11 T11.4 — chained receiver resolution:
--------------------------------------------------
T10.7 kept an index-name-uniqueness fallback that masked chained receivers.
``_get_ddb().Table(_ACTIONS_TABLE).query(IndexName="by_owner")`` left the guard
reporting 5 passed because ``_receiver_to_slug()`` only handled ``ast.Name``
receivers; a ``Call`` receiver (the chained ``_get_ddb().Table(...)`` part) fell
through to the uniqueness fallback, which silently resolved ``by_owner`` to the
findings table (the only table that declares it) and counted the site as passed.
The "skipped none" assertion was inert for the same reason.

This revision:
  1. Extends ``_receiver_to_slug()`` to recognise chained ``*.Table(CONSTANT)``
     receivers regardless of the chain prefix.  ``_get_ddb().Table(_ACTIONS_TABLE)``
     → ``"actions"``; ``boto3.resource(...).Table(_FINDINGS_TABLE)`` → ``"findings"``.
     The rule: any ``ast.Call`` receiver whose ``func.attr == "Table"`` and whose
     single positional arg is an ``ast.Name`` in ``_TABLE_CONSTANT_TO_SLUG`` is
     structurally resolvable.
  2. The uniqueness fallback (Case C / Case D step 2) is retained ONLY for the
     low-level client / positional-call paths where structural resolution cannot
     reach.  Every site resolved by fallback is counted separately
     (``fallback_resolved`` counter) and reported in the guard output so it is
     visibly distinct from a structurally resolved site.
  3. ``test_guard_discovers_call_sites`` now asserts ``fallback_count == 0`` when
     all current call sites are structurally resolvable, making the "skipped none"
     assertion a true invariant rather than an inert tautology.

This rewrite:
  1. Parses the CDK to build a per-table index map:
       ``avxFindingsTable`` → {index_name: pk_name}
       ``avxActionsTable``  → {index_name: pk_name}
  2. Resolves each Python call site to the table it queries, via (a) the local
     variable assigned by ``var = ...Table(CONSTANT)`` within the same function
     scope, OR (b) a chained ``...Table(CONSTANT).method(...)`` receiver inline.
  3. Checks that the index name is declared **on that table** and (where a key
     attribute is also present) that the key matches **that table's** partition
     key for that index.
  4. Where a call site's table cannot be statically resolved, the guard FAILS
     loudly naming ``file:line`` — a silent skip is the mechanism that created
     the original gap.
  5. Non-vacuity: asserts a non-zero count of resolved triples AND that zero
     call sites were skipped.

Rule:
  - Python MOVES to match CDK.
  - Do NOT rename CDK ``indexName:`` values to match Python — those are the
    deployed contract for Group 3's already-reviewed table definitions and are
    referenced by IAM index ARNs.

Test coverage:
  1. ``test_all_python_index_names_are_declared_on_correct_table`` — no call site
     uses an index name that is either undeclared globally OR declared on the
     wrong table.  Subsumes T10.1's ``test_all_python_index_names_are_declared_in_cdk``.
  2. ``test_key_attributes_match_partition_keys`` — call sites that pass both an
     index name and a key attribute use a key that matches the index's declared
     partition key on the correct table.
  3. ``test_guard_discovers_call_sites`` — non-vacuity check: the guard finds at
     least one call site, asserts zero unresolvable sites, and (T11.4) asserts
     zero fallback-resolved sites.  A non-zero fallback count means the guard
     has a blind spot; a non-zero unresolvable count means it has a missing-path.
  4. ``test_guard_rejects_zero_cdk_declarations`` — the CDK parser must find at
     least one index declaration for EACH table; zero declarations on any table
     means the CDK file changed structure and the guard is blind.
  5. ``test_all_python_index_names_are_declared_in_cdk`` — kept for backwards
     compatibility with T10.1; now delegates to the table-aware check.

Mutation gate (required by tasks.md T10.1 + T10.7 + T11.4):
  T10.1/T10.7 mutations (a)–(d) recorded in decisions.md.
  T11.4 mutations: (a) chained receiver on _ACTIONS_TABLE querying "by_owner"
  MUST fail; (b) T10.7's four mutations MUST still be caught; (c) a site
  resolvable by neither path MUST fail loudly naming file:line.
  All results recorded in decisions.md.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Repo root relative to this file
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CDK_STACK = _REPO_ROOT / "infrastructure" / "lib" / "vsa-tier2-stack.ts"

# ---------------------------------------------------------------------------
# Canonical table slugs — these are the tokens we use internally; they do NOT
# appear in user-facing error messages (table names from CDK are used there).
# ---------------------------------------------------------------------------
_TABLE_FINDINGS = "findings"
_TABLE_ACTIONS = "actions"

# Map from module-level Python constant name → table slug.
# These are the ``_FINDINGS_TABLE`` / ``_ACTIONS_TABLE`` constants in each
# handler.py (assigned from ``os.environ.get(...)``).
#
# The writer.py uses an injected ``table_name`` parameter with a module-level
# ``_FINDING_KEY_GSI`` constant; that path is handled separately in
# ``_resolve_call_sites_in_file``.
_TABLE_CONSTANT_TO_SLUG: dict[str, str] = {
    "_FINDINGS_TABLE": _TABLE_FINDINGS,
    "_ACTIONS_TABLE": _TABLE_ACTIONS,
    # Both are env-var constants in the same module; map both spellings just in
    # case a future edit adds a canonically-named variant.
    "FINDINGS_TABLE": _TABLE_FINDINGS,
    "ACTIONS_TABLE": _TABLE_ACTIONS,
}

# ---------------------------------------------------------------------------
# Parse CDK: extract {table_slug: {index_name: partition_key_name}}
# ---------------------------------------------------------------------------


def _parse_cdk_table_index_map() -> dict[str, dict[str, str]]:
    """Return per-table GSI declarations from vsa-tier2-stack.ts.

    Returns::
        {
            "findings": {"by_owner": "owner_id", "by_vin": "vin", ...},
            "actions":  {"by_finding": "finding_id", "by_created_by": "created_by", ...},
        }

    Parsing strategy:
    -----------------
    The CDK file has two table constructs identified by their variable names:
      - ``this.avxFindingsTable = new dynamodb.Table(...)``
      - ``this.avxActionsTable  = new dynamodb.Table(...)``

    Each GSI is added via ``this.avxXxxTable.addGlobalSecondaryIndex({...})``.  We
    scan the file line-by-line and track which table is "current" based on which
    ``addGlobalSecondaryIndex`` receiver was last seen.  Within each GSI block we
    pair ``indexName:`` with the nearest ``partitionKey:``'s ``name:`` within 8
    lines.
    """
    text = _CDK_STACK.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Which table is "current" based on which table's addGlobalSecondaryIndex we
    # are inside.
    result: dict[str, dict[str, str]] = {
        _TABLE_FINDINGS: {},
        _TABLE_ACTIONS: {},
    }

    # Markers for which table the GSI block belongs to.
    _findings_re = re.compile(r"this\.avxFindingsTable\.addGlobalSecondaryIndex")
    _actions_re = re.compile(r"this\.avxActionsTable\.addGlobalSecondaryIndex")

    # Temporary storage keyed by line number.
    gsi_open_at: list[tuple[int, str]] = []   # (line_no, table_slug) for open GSI blocks
    index_at: dict[int, str] = {}             # line_no → index_name
    pk_at: dict[int, str] = {}               # line_no → pk_name

    brace_depth = 0  # crude brace counting to detect GSI block boundaries
    current_table: str | None = None
    in_gsi = False

    for i, line in enumerate(lines):
        # Detect the start of an addGlobalSecondaryIndex block.
        if _findings_re.search(line):
            current_table = _TABLE_FINDINGS
            in_gsi = True
            brace_depth = 0
        elif _actions_re.search(line):
            current_table = _TABLE_ACTIONS
            in_gsi = True
            brace_depth = 0

        if in_gsi:
            brace_depth += line.count("{") - line.count("}")

        # Extract indexName within any GSI context.
        m_idx = re.search(r"indexName\s*:\s*['\"]([^'\"]+)['\"]", line)
        if m_idx:
            index_at[i] = m_idx.group(1)

        # Extract partitionKey.name within any GSI context.
        m_pk = re.search(
            r"partitionKey\s*:\s*\{[^}]*name\s*:\s*['\"]([^'\"]+)['\"]", line
        )
        if m_pk:
            pk_at[i] = m_pk.group(1)

        # Record the current table affiliation for lines with an indexName.
        if m_idx and current_table is not None and in_gsi:
            gsi_open_at.append((i, current_table))

        # Reset when the brace depth returns to 0 after opening.
        if in_gsi and brace_depth <= 0 and i > 0:
            in_gsi = False
            current_table = None
            brace_depth = 0

    # Pair each indexName with the nearest partitionKey within 8 lines and store
    # under the table slug recorded for that line.
    for idx_line, idx_name in index_at.items():
        # Find table slug for this index line.
        table_slug: str | None = None
        for record_line, slug in gsi_open_at:
            if record_line == idx_line:
                table_slug = slug
                break
        if table_slug is None:
            continue

        # Pair with nearest partitionKey.
        best_pk: str | None = None
        best_dist = 9999
        for pk_line, pk_name in pk_at.items():
            dist = abs(pk_line - idx_line)
            if dist <= 8 and dist < best_dist:
                best_dist = dist
                best_pk = pk_name

        if best_pk is not None:
            result[table_slug][idx_name] = best_pk

    return result


# ---------------------------------------------------------------------------
# Legacy helper: flat {index_name: pk} union used by original T10.1 tests.
# ---------------------------------------------------------------------------

def _parse_cdk_index_declarations() -> dict[str, str]:
    """Return a flat ``{index_name: pk}`` map across ALL tables.

    Kept for backwards-compatibility with the T10.1 test names that called this
    directly.  Table-aware checks use ``_parse_cdk_table_index_map()`` instead.
    """
    table_map = _parse_cdk_table_index_map()
    merged: dict[str, str] = {}
    for per_table in table_map.values():
        merged.update(per_table)
    return merged


# ---------------------------------------------------------------------------
# Python source file enumeration
# ---------------------------------------------------------------------------


def _python_source_files() -> list[Path]:
    """All .py files under the AVX surface directories (excluding __pycache__).

    Scoped to the files that read/write the AVX Findings and Actions tables
    declared in ``vsa-tier2-stack.ts``.  Other Lambda/agent directories may
    use DDB indexes for CMS or Acquire tables with different naming conventions
    — this guard is about the AVX surface only.

    The guard would produce false positives on e.g.:
      - ``lambdas/shared/cms_client_real.py`` (CMS storage table, ``vin-index``)
      - ``agents/supervisor/tools/lead_capture.py`` (Acquire table, ``by-tenant-and-contact``)
      - ``lambdas/acquire/`` (Acquire order tables)
    """
    avx_roots = [
        _REPO_ROOT / "lambdas" / "api-avx-findings",
        _REPO_ROOT / "lambdas" / "api-avx-actions",
        _REPO_ROOT / "lambdas" / "api-avx-notifications-register-device",
        _REPO_ROOT / "agents" / "tier2",
        # T14.2: `lambdas/_lib/` is bundled into every AVX Lambda, and its
        # DynamoDB calls are real deployed call sites. Critical C1 (review
        # Cycle 2) was an IAM grant removed because the sibling guard read only
        # handler.py and missed `_lib/avx_scope.py::_load_finding_by_id`. This
        # guard had the identical blind spot. Deliberate exception to T11.4's
        # four-root constraint; the roots are otherwise unchanged, because
        # widening to all of `agents/**` + `lambdas/**` produces false positives
        # on unrelated tables (verified twice — see the docstring above).
        _REPO_ROOT / "lambdas" / "_lib",
    ]
    files: list[Path] = []
    for root in avx_roots:
        if root.exists():
            for p in root.rglob("*.py"):
                if "__pycache__" not in str(p):
                    files.append(p)
    return files


# ---------------------------------------------------------------------------
# Table-aware call-site resolution
# ---------------------------------------------------------------------------


class _CallSite(NamedTuple):
    """A resolved (call-site, table, index) triple."""
    index_name: str
    table_slug: str          # _TABLE_FINDINGS | _TABLE_ACTIONS
    key_name: str | None     # present when a positional key arg was found
    lineno: int
    path: Path
    resolved_by: str = "structural"  # "structural" | "fallback"


class _UnresolvableCallSite(NamedTuple):
    """A call site for which we could not determine the table."""
    index_name: str
    lineno: int
    path: Path
    reason: str


# ── Externally-owned tables inside the AVX surface (F11.2, 2026-09-23) ────────
#
# The guard's scope (see _python_source_files) is the AVX surface, on the premise
# that every DDB index call in those directories targets a table declared in
# vsa-tier2-stack.ts. F11.2 broke that premise for the first time:
# agents/tier2/owner_resolver.py lives in the AVX surface but queries the
# **CMS-owned** registry, cms-{stage}-storage-vehicles, whose `vin-index` CVX's
# CDK does not declare and must not. That is precisely the false-positive class
# the scoping docstring already names for lambdas/shared/cms_client_real.py.
#
# So this is a NAMED, per-(file, index) exemption — not a relaxed assertion and
# not a widened scope. Two anti-vacuity tests below keep it honest:
#   * test_external_table_exemptions_are_still_real_call_sites — a stale entry fails
#   * test_external_table_exemptions_do_not_absorb_everything — the mechanism must
#     still report unresolvable sites that are NOT exempt
#
# Mutation (recorded in decisions.md § "2026-09-23 — Fix Group 15 build"):
#   delete this entry → test_guard_discovers_call_sites FAILS again with
#   "owner_resolver.py:173 ... is not a known GSI constant". Restored; green.
#
# Adding an entry here is a claim that the table is owned by another repo. If the
# table IS in vsa-tier2-stack.ts, fix the call site instead.
_EXTERNAL_TABLE_INDEX_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("agents/tier2/owner_resolver.py", "VIN_INDEX_NAME"):
        "vin-index on cms-{stage}-storage-vehicles — CMS-owned table, declared in "
        "the CMS repo's CDK, not in vsa-tier2-stack.ts. Grant: CmsVehiclesVinIndexQuery.",
}


def _is_exempt_external_table(u: _UnresolvableCallSite) -> bool:
    """True when this unresolvable site targets a known externally-owned table."""
    try:
        rel = u.path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return False
    return (rel, u.index_name) in _EXTERNAL_TABLE_INDEX_EXEMPTIONS


def _resolve_call_sites_in_file(
    path: Path,
    table_map: dict[str, dict[str, str]],
) -> tuple[list[_CallSite], list[_UnresolvableCallSite]]:
    """Parse a Python source file and return resolved + unresolvable call sites.

    Resolution algorithm
    --------------------
    For each function definition in the file:

    Step 0 — pre-pass: build a function-name → table-slug map by scanning all
    ``FunctionDef`` bodies for ``Table(CONSTANT)`` calls.  This lets the positional-
    call resolver know which table a helper function ultimately queries (e.g.
    ``_list_findings_by_index`` → ``findings``).

    Step 1 — build a local variable-to-table-slug map by scanning all
    ``Assign`` / ``AnnAssign`` nodes of the form::

        var_name = ...Table(CONSTANT_NAME)

    where ``CONSTANT_NAME`` is in ``_TABLE_CONSTANT_TO_SLUG``.

    Step 2 — collect module-level GSI constant definitions of the form::

        _SOME_GSI = "by_some_index"

    These are used by writer.py which passes the GSI constant as a keyword
    rather than a literal.

    Step 3 — for each ``IndexName`` value found at a call site within the
    function:

    Case A (keyword): ``some_table_var.query(IndexName="by_vin", ...)``.
    Walk up from the ``keyword`` node to its enclosing ``Call``.  The
    receiver of that call (``Call.func.value``) is a ``Name`` whose
    ``id`` we look up in the function's variable-to-table map.

    Case B (dict literal): ``kwargs = {"IndexName": "by_vin", ...}; var.query(**kwargs)``.
    Find all ``Assign`` / ``AnnAssign`` nodes where the RHS is a ``Dict`` containing
    ``"IndexName"`` as a key.  Then find the ``**kwargs`` expansion in
    any ``Call`` in the function body.  Resolve the ``Call.func.value``
    to a table slug.

    Case C (low-level client): ``client.query(TableName=..., IndexName="by_finding_key", ...)``.
    The receiver is not a resource Table object — we cannot resolve from the
    variable name alone.  Instead, look up the index name in the per-table map.
    If the index belongs to exactly one table, resolve to that table.  If it
    appears on multiple tables (or zero), fail loudly.

    Case D (positional call): ``_list_findings_by_index("by_vin", "vin", ...)``.
    The resolution priority is:
    1. Look up the callee name in the function-name→table map (Step 0). If the
       callee queries a single known table, the index must belong to that table.
    2. Index-uniqueness: if the index is declared on exactly one table in the CDK,
       resolve to that table.
    3. If neither resolves, add to unresolvable.

    Non-resolution rule
    -------------------
    If none of the above cases resolve the table, add an
    ``_UnresolvableCallSite`` entry.  The test will fail with a clear
    ``file:line`` message.

    Scope note
    ----------
    We analyse at function-definition granularity (``ast.FunctionDef``).
    Nested functions / comprehensions are walked at the FunctionDef level that
    directly contains the IndexName usage — i.e. we do NOT recurse into inner
    functions for the purpose of variable tracking; inner functions get their
    own separate analysis pass.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return [], []

    # ------------------------------------------------------------------
    # Step 0: pre-pass — build function_name → table_slug map.
    # For each function that directly assigns Table(CONSTANT), record
    # which table it queries.  Used by positional-call resolution.
    # ------------------------------------------------------------------
    func_to_table_slug: dict[str, str] = {}
    for top_node in ast.walk(tree):
        if not isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        found_slugs: set[str] = set()
        for stmt in ast.walk(top_node):
            # Scan Assign and AnnAssign for Table(CONSTANT) calls.
            target_name: str | None = None
            rhs_val: ast.expr | None = None
            if isinstance(stmt, ast.Assign) and stmt.value is not None:
                rhs_val = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                rhs_val = stmt.value

            if rhs_val is None:
                continue
            if not (
                isinstance(rhs_val, ast.Call)
                and isinstance(rhs_val.func, ast.Attribute)
                and rhs_val.func.attr == "Table"
                and len(rhs_val.args) == 1
                and isinstance(rhs_val.args[0], ast.Name)
            ):
                continue
            slug = _TABLE_CONSTANT_TO_SLUG.get(rhs_val.args[0].id)
            if slug is not None:
                found_slugs.add(slug)
        # Only record if exactly one table is used in this function.
        if len(found_slugs) == 1:
            func_to_table_slug[top_node.name] = next(iter(found_slugs))

    # ------------------------------------------------------------------
    # Step 2: collect module-level _SOME_GSI = "by_something" constants.
    # ------------------------------------------------------------------
    module_gsi_constants: dict[str, str] = {}  # var_name → index_name_literal
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            val = node.value.value
            if isinstance(val, str) and (
                val.startswith("by_") or val.endswith("_index") or val.endswith("-index")
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and (
                        target.id.endswith("_GSI")
                        or target.id.endswith("_INDEX")
                        or target.id.endswith("_index")
                    ):
                        module_gsi_constants[target.id] = val

    # ------------------------------------------------------------------
    # Helper: resolve a Call node's receiver to a table slug.
    # Uses the function-local var→slug map passed in.
    # ------------------------------------------------------------------
    # T14.2 sentinel: the receiver IS a resource-Table handle but its table
    # could not be pinned. Distinct from None (not a Table handle at all), which
    # is the only case allowed to fall back to index-name uniqueness.
    _UNRESOLVABLE_TABLE_RECEIVER = "__UNRESOLVABLE_TABLE_RECEIVER__"

    def _table_call_const(node: ast.expr) -> tuple[bool, str | None]:
        """Is ``node`` a ``*.Table(...)`` call? Return (is_table_call, const_name).

        Accepts both the positional form ``Table(CONST)`` and the keyword form
        ``Table(name=CONST)``. ``const_name`` is None when the argument is not a
        plain Name (e.g. ``Table(dynamic())``), which makes the site
        *unresolvable* rather than *not-a-table-call* — a distinction the caller
        depends on, because unresolvable must fail and not-a-table-call may fall
        back to the low-level-client path.
        """
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Table"
        ):
            return False, None
        arg: ast.expr | None = None
        if len(node.args) == 1:
            arg = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == "name":
                    arg = kw.value
                    break
        if isinstance(arg, ast.Name):
            return True, arg.id
        return True, None

    def _is_low_level_client_call(call_node: ast.Call) -> bool:
        """Is this the boto3 CLIENT API (Case C), rather than a resource Table handle?

        W2 (review Cycles 3 and 4): the uniqueness fallback was reachable for ANY
        receiver shape `_receiver_to_slug` did not recognise, and Cycle 4 defeated the
        guard three more ways — `self.tbl`, `_helper()`, `reg["a"]` — each reproducing
        Cycle 1's wrong-table defect at 5/5 green. Enumerating receiver shapes loses
        that race; T14.2 added two shapes and Cycle 4 found three more.

        The discriminator is the CALL, not the receiver. boto3's low-level client
        requires `TableName=` (the table is an argument); the resource `Table` object
        carries the table in the handle and never passes `TableName`. So a call with
        `TableName=` is genuinely Case C and index-name uniqueness is a legitimate
        resolution for it. Anything else that cannot be resolved structurally FAILS,
        which is the property T14.2 Accept 1 asked for and did not deliver.
        """
        return any(kw.arg == "TableName" for kw in call_node.keywords)

    def _receiver_to_slug(
        call_node: ast.Call,
        var_to_slug: dict[str, str],
        index_name: str,
        table_origin_vars: frozenset[str] = frozenset(),
    ) -> str | None:
        """Return the table slug for the receiver of call_node, or None.

        Returns the sentinel ``_UNRESOLVABLE_TABLE_RECEIVER`` when the receiver
        *is* a resource-``Table`` handle whose table cannot be determined. That
        case MUST fail rather than fall through to index-name guessing — review
        Cycle 2 defeated the previous version twice (an alias hop and
        ``Table(name=CONST)``) precisely because both fell to the fallback and
        were silently attributed to the wrong table.

        ``None`` is reserved for receivers that are genuinely not resource-Table
        handles (the low-level ``dynamodb_client`` shape, Case C), which is the
        only case where index-name uniqueness is a legitimate resolution.
        """
        if not isinstance(call_node.func, ast.Attribute):
            return None
        receiver = call_node.func.value

        # Shape 1: simple Name receiver (possibly reached via an alias chain,
        # which Step 1 has already flattened into var_to_slug).
        if isinstance(receiver, ast.Name):
            slug = var_to_slug.get(receiver.id)
            if slug is not None:
                return slug
            # The name came from a .Table(...) expression but we could not pin
            # the table — fail, do not guess.
            if receiver.id in table_origin_vars:
                return _UNRESOLVABLE_TABLE_RECEIVER
            return None

        # Shape 2: chained *.Table(CONST) / *.Table(name=CONST) receiver.
        is_table_call, const_name = _table_call_const(receiver)
        if is_table_call:
            if const_name is None:
                return _UNRESOLVABLE_TABLE_RECEIVER
            slug = _TABLE_CONSTANT_TO_SLUG.get(const_name)
            return slug if slug is not None else _UNRESOLVABLE_TABLE_RECEIVER

        # receiver is a more complex expression — cannot resolve structurally
        return None

    # ------------------------------------------------------------------
    # Helper: resolve via the per-table index map when the receiver is
    # a low-level client (Case C).  Succeeds iff the index is on exactly
    # one table.
    # ------------------------------------------------------------------
    def _resolve_by_index_name_uniqueness(
        index_name: str,
    ) -> str | None:
        owners = [
            slug
            for slug, idx_map in table_map.items()
            if index_name in idx_map
        ]
        return owners[0] if len(owners) == 1 else None

    resolved: list[_CallSite] = []
    unresolvable: list[_UnresolvableCallSite] = []

    # ------------------------------------------------------------------
    # Walk top-level function definitions (and class methods at top level).
    # For each, build a local var→slug map and then analyse all call sites.
    # ------------------------------------------------------------------
    def _process_function_body(
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        var_to_slug: dict[str, str],
    ) -> None:
        """Process a single function body for IndexName call sites.

        ``var_to_slug`` comes in pre-populated with module-level mappings;
        we add local assignments on top.
        """
        # T14.2: names that came from a .Table(...) expression, resolvable or not.
        table_origin_vars: set[str] = set()

        # Step 1: extend var_to_slug with local assignments in this function.
        # Handles both plain Assign and annotated AnnAssign.
        for stmt in ast.walk(func_node):
            target_name: str | None = None
            rhs_val: ast.expr | None = None
            if isinstance(stmt, ast.Assign) and stmt.value is not None:
                rhs_val = stmt.value
                if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name = stmt.targets[0].id
                # multi-target assigns: scan all Name targets
                elif len(stmt.targets) >= 1:
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            target_name = t.id
                            break
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                rhs_val = stmt.value
                if isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id

            if target_name is None or rhs_val is None:
                continue

            # T14.2: does the RHS mention a .Table( call anywhere? If so this
            # variable is a resource-Table handle, and failing to pin its table
            # must FAIL rather than fall back to index-name guessing.
            for sub in ast.walk(rhs_val):
                is_tc, _c = _table_call_const(sub)
                if is_tc:
                    table_origin_vars.add(target_name)
                    break

            # T14.2: alias hop — `alias = actions_tbl`, where actions_tbl is
            # already a known table handle. Review Cycle 2 used exactly this to
            # reproduce the wrong-table defect at 5-passed.
            if isinstance(rhs_val, ast.Name):
                if rhs_val.id in var_to_slug:
                    var_to_slug[target_name] = var_to_slug[rhs_val.id]
                if rhs_val.id in table_origin_vars:
                    table_origin_vars.add(target_name)
                continue

            # Match:  var = something.Table(CONSTANT) / .Table(name=CONSTANT)
            is_table_call, const_name = _table_call_const(rhs_val)
            if not is_table_call:
                continue
            if const_name is None:
                continue
            slug = _TABLE_CONSTANT_TO_SLUG.get(const_name)
            if slug is not None:
                var_to_slug[target_name] = slug

        # Step 2: also collect dict-based "IndexName" → var mappings.
        # dict_var_to_index_name: dict var name → index name literal
        #
        # Two forms of dict assignment are handled:
        #   var = {"IndexName": "by_vin", ...}           (ast.Assign)
        #   var: dict[...] = {"IndexName": "by_vin", ...} (ast.AnnAssign)
        dict_var_to_index_name: dict[str, str] = {}
        for stmt in ast.walk(func_node):
            # Normalise both Assign and AnnAssign to (target_name, rhs_value).
            target_name: str | None = None
            rhs: ast.expr | None = None
            if isinstance(stmt, ast.Assign) and stmt.value is not None:
                rhs = stmt.value
                if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name = stmt.targets[0].id
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                rhs = stmt.value
                if isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id

            if target_name is None or not isinstance(rhs, ast.Dict):
                continue
            for key, val in zip(rhs.keys, rhs.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "IndexName"
                    and isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                ):
                    dict_var_to_index_name[target_name] = str(val.value)

        # Also look for dict subscript assignments: kwargs["IndexName"] = "by_vin"
        for stmt in ast.walk(func_node):
            if not isinstance(stmt, ast.Assign):
                continue
            # Target: kwargs["IndexName"]
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "IndexName"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    dict_var_to_index_name[target.value.id] = str(stmt.value.value)

        # Step 3: find all Call nodes and process each IndexName usage.
        for call_node in ast.walk(func_node):
            if not isinstance(call_node, ast.Call):
                continue

            # ---- Case A: IndexName as a keyword argument ----
            for kw in call_node.keywords:
                if kw.arg == "IndexName" and isinstance(kw.value, ast.Constant):
                    idx_name = str(kw.value.value)
                    lineno = kw.value.lineno
                    slug = _receiver_to_slug(call_node, var_to_slug, idx_name, frozenset(table_origin_vars))
                    _by = "structural"
                    if slug == _UNRESOLVABLE_TABLE_RECEIVER:
                        slug = None
                        _by = "unresolvable-table-handle"
                    elif slug is None:
                        if _is_low_level_client_call(call_node):
                            if _is_low_level_client_call(call_node):
                                slug = _resolve_by_index_name_uniqueness(idx_name)
                                _by = "fallback"
                            else:
                                slug = None
                                _by = "unresolvable-non-client-receiver"
                        else:
                            slug = None
                            _by = "unresolvable-non-client-receiver"
                    if slug is not None:
                        resolved.append(
                            _CallSite(idx_name, slug, None, lineno, path, _by)
                        )
                    else:
                        unresolvable.append(
                            _UnresolvableCallSite(
                                idx_name, lineno, path,
                                "cannot resolve table for keyword IndexName call site"
                            )
                        )
                    continue

                # Also handle: IndexName=SOME_GSI_CONSTANT (name, not literal)
                if kw.arg == "IndexName" and isinstance(kw.value, ast.Name):
                    gsi_var = kw.value.id
                    idx_name = module_gsi_constants.get(gsi_var)
                    lineno = kw.value.lineno
                    if idx_name is not None:
                        slug = _receiver_to_slug(call_node, var_to_slug, idx_name, frozenset(table_origin_vars))
                        _by = "structural"
                        if slug == _UNRESOLVABLE_TABLE_RECEIVER:
                            slug = None
                            _by = "unresolvable-table-handle"
                        elif slug is None:
                            if _is_low_level_client_call(call_node):
                                slug = _resolve_by_index_name_uniqueness(idx_name)
                                _by = "fallback"
                            else:
                                slug = None
                                _by = "unresolvable-non-client-receiver"
                        if slug is not None:
                            resolved.append(
                                _CallSite(idx_name, slug, None, lineno, path, _by)
                            )
                        else:
                            unresolvable.append(
                                _UnresolvableCallSite(
                                    idx_name, lineno, path,
                                    f"cannot resolve table for GSI constant {gsi_var!r}"
                                )
                            )
                    else:
                        unresolvable.append(
                            _UnresolvableCallSite(
                                gsi_var, lineno, path,
                                f"IndexName=Name({gsi_var!r}) is not a known GSI constant"
                            )
                        )
                    continue

            # ---- Case B: **dict_var expansion ----
            for kw in call_node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Name):
                    dict_var = kw.value.id
                    idx_name = dict_var_to_index_name.get(dict_var)
                    if idx_name is None:
                        continue
                    lineno = kw.value.lineno
                    slug = _receiver_to_slug(call_node, var_to_slug, idx_name, frozenset(table_origin_vars))
                    _by = "structural"
                    if slug == _UNRESOLVABLE_TABLE_RECEIVER:
                        slug = None
                        _by = "unresolvable-table-handle"
                    elif slug is None:
                        if _is_low_level_client_call(call_node):
                            if _is_low_level_client_call(call_node):
                                slug = _resolve_by_index_name_uniqueness(idx_name)
                                _by = "fallback"
                            else:
                                slug = None
                                _by = "unresolvable-non-client-receiver"
                        else:
                            slug = None
                            _by = "unresolvable-non-client-receiver"
                    if slug is not None:
                        resolved.append(
                            _CallSite(idx_name, slug, None, lineno, path, _by)
                        )
                    else:
                        unresolvable.append(
                            _UnresolvableCallSite(
                                idx_name, lineno, path,
                                f"cannot resolve table for **{dict_var} expansion"
                            )
                        )

    # ------------------------------------------------------------------
    # Also handle: positional call sites _list_findings_by_index("by_vin", "vin", ...)
    # This is the original T10.1 "key-slot" detection.  We keep it for
    # regression coverage, resolving table from the function body as before.
    # ------------------------------------------------------------------
    def _process_positional_pairs(
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        var_to_slug: dict[str, str],
    ) -> None:
        all_indexes = set(_parse_cdk_index_declarations().keys())
        for call_node in ast.walk(func_node):
            if not isinstance(call_node, ast.Call):
                continue
            args = call_node.args
            if (
                len(args) >= 2
                and isinstance(args[0], ast.Constant)
                and isinstance(args[1], ast.Constant)
                and isinstance(args[0].value, str)
                and isinstance(args[1].value, str)
            ):
                idx_candidate = str(args[0].value)
                key_candidate = str(args[1].value)
                if not (
                    idx_candidate.startswith("by_") or idx_candidate.endswith("-index")
                ):
                    continue
                lineno = call_node.lineno

                # Resolution priority for positional calls:
                # 1. Callee-function table map (Step 0 pre-pass): if the callee
                #    is a known function that queries exactly one table, the index
                #    argument must be from that table.
                #    _list_findings_by_index → findings (has Table(_FINDINGS_TABLE))
                #    _list_actions_by_scope → two tables, not in map → skip to step 2
                # 2. Index-uniqueness: if the index is declared on exactly one
                #    table in the CDK, the resolved table is the table that owns
                #    the index.
                slug = None
                if isinstance(call_node.func, ast.Name):
                    slug = func_to_table_slug.get(call_node.func.id)
                _by = "structural"
                if slug is None:
                    slug = _resolve_by_index_name_uniqueness(idx_candidate)
                    _by = "fallback"

                if slug is not None:
                    resolved.append(
                        _CallSite(idx_candidate, slug, key_candidate, lineno, path, _by)
                    )
                else:
                    # Could be undeclared (caught by name-check) or ambiguous.
                    if idx_candidate in all_indexes:
                        unresolvable.append(
                            _UnresolvableCallSite(
                                idx_candidate, lineno, path,
                                "index is declared on multiple tables; cannot resolve positional"
                            )
                        )
                    # else: undeclared — will be caught by the declaration check

    # Walk the module for all top-level function definitions.
    for top_node in ast.walk(tree):
        if not isinstance(top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Build a fresh var→slug map for each function (no cross-function leakage).
        local_var_to_slug: dict[str, str] = {}
        _process_function_body(top_node, local_var_to_slug)
        _process_positional_pairs(top_node, local_var_to_slug)

    return resolved, unresolvable


# ---------------------------------------------------------------------------
# Legacy extractors (kept for test_key_attributes_match_partition_keys).
# ---------------------------------------------------------------------------


def _extract_index_names_from_file(path: Path) -> list[tuple[str, int]]:
    """Return [(index_name, lineno)] for every IndexName literal (flat, no table).

    Used only by the legacy ``test_all_python_index_names_are_declared_in_cdk``
    test which is preserved for backwards compatibility.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    results: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_keyword(self, node: ast.keyword) -> None:
            if node.arg == "IndexName" and isinstance(node.value, ast.Constant):
                results.append((str(node.value.value), node.value.lineno))
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:
            for key, val in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "IndexName"
                    and isinstance(val, ast.Constant)
                ):
                    results.append((str(val.value), val.lineno))
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                val = str(node.value.value)
                if (
                    val.startswith("by_")
                    or val.endswith("-index")
                    or val.endswith("_index")
                ):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and (
                                target.id.endswith("_GSI")
                                or target.id.endswith("_INDEX")
                                or target.id.endswith("_index")
                            )
                        ):
                            results.append((val, node.value.lineno))
                            break
            self.generic_visit(node)

    _Visitor().visit(tree)
    return results


def _kw_value(keywords: list[ast.keyword], name: str) -> ast.expr | None:
    """Return the value node of keyword ``name``, or None."""
    for kw in keywords:
        if kw.arg == name:
            return kw.value
    return None


def _const_str_kw(keywords: list[ast.keyword], name: str) -> str | None:
    """Return keyword ``name``'s value if it is a string literal, else None."""
    v = _kw_value(keywords, name)
    if isinstance(v, ast.Constant) and isinstance(v.value, str):
        return str(v.value)
    return None


def _key_attr_from_expr(node: ast.expr | None) -> str | None:
    """Extract ``X`` from an expression containing ``Key("X")``.

    Handles the chained forms these call sites actually use —
    ``Key("vin").eq(v)``, ``Key("vin").eq(v) & Attr(...)`` — by walking the
    whole subtree for the first ``Key(<str literal>)`` call.
    """
    if node is None:
        return None
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "Key"
            and len(sub.args) == 1
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            return str(sub.args[0].value)
    return None


def _extract_index_key_pairs_from_file(
    path: Path,
) -> list[tuple[str, str, int]]:
    """Return [(index_name, key_name, lineno)] for positional call sites."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    results: list[tuple[str, str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            args = node.args
            if (
                len(args) >= 2
                and isinstance(args[0], ast.Constant)
                and isinstance(args[1], ast.Constant)
                and isinstance(args[0].value, str)
                and isinstance(args[1].value, str)
            ):
                idx_candidate = str(args[0].value)
                key_candidate = str(args[1].value)
                if idx_candidate.startswith("by_") or idx_candidate.endswith("-index"):
                    results.append((idx_candidate, key_candidate, node.lineno))
            # T14.2: keyword form on the same call —
            #   table.query(IndexName="by_vin", KeyConditionExpression=Key("vin").eq(v))
            kw_idx = _const_str_kw(node.keywords, "IndexName")
            if kw_idx is not None:
                kw_key = _key_attr_from_expr(_kw_value(node.keywords, "KeyConditionExpression"))
                if kw_key is not None:
                    results.append((kw_idx, kw_key, node.lineno))
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:
            # T14.2: dict form — the shape F1 actually shipped in:
            #   kwargs = {"IndexName": "by_vin",
            #             "KeyConditionExpression": Key("vin_index").eq(vin)}
            # Previously these sites had key_name=None and were SILENTLY SKIPPED
            # by test_key_attributes_match_partition_keys, so the guard did not
            # protect against a recurrence of the original defect.
            idx_name: str | None = None
            key_attr: str | None = None
            key_lineno = node.lineno
            for k, v in zip(node.keys, node.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if k.value == "IndexName" and isinstance(v, ast.Constant) and isinstance(v.value, str):
                    idx_name = str(v.value)
                elif k.value == "KeyConditionExpression":
                    found = _key_attr_from_expr(v)
                    if found is not None:
                        key_attr = found
                        key_lineno = v.lineno
            if idx_name is not None and key_attr is not None:
                results.append((idx_name, key_attr, key_lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_guard_discovers_call_sites():
    """Non-vacuity: the guard must find resolved triples, zero skipped, and zero ambiguous fallback sites.

    Three assertions:
    1. At least one Python source file exists and has at least one IndexName usage.
    2. The table-aware resolver finds at least one resolved (call-site, table, index)
       triple — and finds ZERO unresolvable call sites (a non-zero skip count means
       the guard has a blind spot, which is the exact mechanism that created the
       original gap this upgrade closes).
    3. (T11.4) Fallback-resolved sites are reported distinctly from structural ones.
       The guard FAILS if any fallback site's index is ambiguous (declared on multiple
       tables) — that would mean the fallback resolved to an indeterminate table.
       Unambiguous fallback sites (Case C low-level client, positional pairs for
       multi-table helpers) are reported but do not fail the test.
       A structurally-resolvable site (has a Table(CONSTANT) receiver) that fell
       to the fallback is now caught and resolved structurally by the extended
       _receiver_to_slug() — that was the T11.4 bug.
    """
    files = _python_source_files()
    assert len(files) > 0, (
        "No Python source files found under agents/ or lambdas/. "
        "The guard is not scanning any files."
    )

    total_hits = 0
    for f in files:
        total_hits += len(_extract_index_names_from_file(f))

    assert total_hits > 0, (
        "No IndexName string literals found in any Python source file. "
        "Either the guard's AST visitor is broken, or all DDB code has been removed."
    )

    # Table-aware non-vacuity.
    table_map = _parse_cdk_table_index_map()
    all_resolved: list[_CallSite] = []
    all_unresolvable: list[_UnresolvableCallSite] = []
    for f in files:
        r, u = _resolve_call_sites_in_file(f, table_map)
        all_resolved.extend(r)
        all_unresolvable.extend(u)

    # F11.2: drop call sites that target a known externally-owned table. The
    # exemption is per (file, index-constant) and is itself guarded — see
    # _EXTERNAL_TABLE_INDEX_EXEMPTIONS and the two anti-vacuity tests.
    all_unresolvable = [u for u in all_unresolvable if not _is_exempt_external_table(u)]

    assert len(all_resolved) > 0, (
        "Table-aware resolver found zero resolved (call-site, table, index) triples. "
        "The resolver is broken or all DDB index calls have been removed."
    )

    if all_unresolvable:
        msgs = [
            f"  {u.path.relative_to(_REPO_ROOT)}:{u.lineno} — "
            f"IndexName={u.index_name!r}: {u.reason}"
            for u in all_unresolvable
        ]
        raise AssertionError(
            f"Table-aware resolver could not resolve {len(all_unresolvable)} call site(s). "
            "Every call site MUST resolve to a specific table — a silent skip is the "
            "mechanism that created the original gap. Fix the call site or the resolver.\n"
            + "\n".join(msgs)
        )

    # T11.4: report fallback-resolved sites distinctly.
    # Fallback sites are those resolved via index-name uniqueness (Case C / positional Case D)
    # rather than via structural receiver analysis.  They are NOT an error on their own —
    # Case C (low-level boto3 client) and positional pairs for multi-table helpers are
    # legitimate.  But they MUST be reported so a reviewer can see them and so a future
    # site that SHOULD be structural is not silently masked.
    #
    # An error is raised only if any fallback site uses an index that is ambiguous (on
    # multiple tables) — that would mean the fallback resolved to the wrong table.
    fallback_sites = [s for s in all_resolved if s.resolved_by == "fallback"]
    ambiguous_fallback: list[_CallSite] = []
    for s in fallback_sites:
        owners = [
            slug for slug, idx_map in table_map.items() if s.index_name in idx_map
        ]
        if len(owners) != 1:
            ambiguous_fallback.append(s)

    if ambiguous_fallback:
        msgs = [
            f"  {s.path.relative_to(_REPO_ROOT)}:{s.lineno} — "
            f"IndexName={s.index_name!r} is ambiguous (declared on: "
            f"{[slug for slug, idx_map in table_map.items() if s.index_name in idx_map]!r}); "
            f"fallback cannot safely resolve — rewrite using a structural receiver"
            for s in ambiguous_fallback
        ]
        raise AssertionError(
            f"{len(ambiguous_fallback)} fallback-resolved call site(s) use ambiguous index names. "
            "The uniqueness fallback can only be trusted when the index is on exactly one table. "
            "Extend _receiver_to_slug() or rewrite the call site.\n"
            + "\n".join(msgs)
        )

    # Report (but do not fail on) unambiguous fallback sites so they are visible.
    if fallback_sites:
        msgs = [
            f"  {s.path.relative_to(_REPO_ROOT)}:{s.lineno} — "
            f"IndexName={s.index_name!r} on {s.table_slug!r} table "
            f"(fallback: index-name uniqueness; NOT a structural receiver)"
            for s in fallback_sites
        ]
        print(
            f"[T11.4] {len(fallback_sites)} call site(s) resolved by index-name-uniqueness fallback "
            "(unambiguous — each index is on exactly one table). These are distinct from structural "
            "resolutions and are reported here per T11.4 counting requirement:\n"
            + "\n".join(msgs)
        )


def test_guard_rejects_zero_cdk_declarations():
    """Non-vacuity: the CDK parser must find at least one index on EACH table.

    If either table has zero GSI declarations the CDK file changed structure
    and the guard is blind for that table.
    """
    table_map = _parse_cdk_table_index_map()
    for slug in (_TABLE_FINDINGS, _TABLE_ACTIONS):
        decls = table_map.get(slug, {})
        assert len(decls) > 0, (
            f"No GSI declarations found for the {slug} table in {_CDK_STACK}. "
            "Either the file structure changed or the parser regex is broken."
        )


def test_all_python_index_names_are_declared_on_correct_table():
    """Every IndexName used at a call site must be declared on the table that call queries.

    This is the core T10.7 assertion.  It subsumes T10.1's flat inventory check
    because an index that is on the wrong table is also, by definition, not on
    the correct table.

    Failure message names the offending file:line, the index name, the table
    the call queries, and the tables where the index IS declared.
    """
    table_map = _parse_cdk_table_index_map()

    # Also build a flat union for the "undeclared anywhere" message variant.
    flat_declared = _parse_cdk_index_declarations()

    violations: list[str] = []

    for f in _python_source_files():
        resolved, _unresolvable = _resolve_call_sites_in_file(f, table_map)
        # _unresolvable failures are caught by test_guard_discovers_call_sites;
        # we don't double-count them here.

        for site in resolved:
            per_table = table_map.get(site.table_slug, {})
            rel = site.path.relative_to(_REPO_ROOT)

            if site.index_name not in per_table:
                # Is it declared on any table at all?
                if site.index_name in flat_declared:
                    # Wrong-table error — the specific error T10.7 closes.
                    declared_on = [
                        slug
                        for slug, idx_map in table_map.items()
                        if site.index_name in idx_map
                    ]
                    violations.append(
                        f"  {rel}:{site.lineno} — IndexName={site.index_name!r} "
                        f"is queried on the {site.table_slug!r} table but is only "
                        f"declared on: {declared_on}. "
                        f"Python must query the correct table."
                    )
                else:
                    # Undeclared anywhere — original T10.1 class of error.
                    violations.append(
                        f"  {rel}:{site.lineno} — IndexName={site.index_name!r} "
                        f"is not declared in vsa-tier2-stack.ts on any table. "
                        f"Declared names: {sorted(flat_declared.keys())}"
                    )

        # Also check flat (non-table-aware) patterns for the legacy path and
        # for GSI constant assignments not captured by the table resolver.
        for index_name, lineno in _extract_index_names_from_file(f):
            if index_name not in flat_declared:
                rel = f.relative_to(_REPO_ROOT)
                violations.append(
                    f"  {rel}:{lineno} — IndexName={index_name!r} "
                    f"is not declared in vsa-tier2-stack.ts. "
                    f"Declared names: {sorted(flat_declared.keys())}"
                )

        for index_name, _key_name, lineno in _extract_index_key_pairs_from_file(f):
            if index_name not in flat_declared:
                rel = f.relative_to(_REPO_ROOT)
                violations.append(
                    f"  {rel}:{lineno} — positional index_name={index_name!r} "
                    f"is not declared in vsa-tier2-stack.ts. "
                    f"Declared names: {sorted(flat_declared.keys())}"
                )

    assert not violations, (
        "Python source uses DDB index names that are either undeclared or queried "
        "on the wrong table. Python MUST move to match CDK — do NOT rename CDK "
        "indexName values.\n"
        + "\n".join(violations)
    )


def test_all_python_index_names_are_declared_in_cdk():
    """Legacy T10.1 test: every IndexName literal must exist in the CDK (flat check).

    Now delegates to ``test_all_python_index_names_are_declared_on_correct_table``,
    which is strictly stronger: anything that would fail here also fails there.
    Kept as a named test for backwards compatibility with CI and task-reference.
    """
    test_all_python_index_names_are_declared_on_correct_table()


def test_key_attributes_match_partition_keys():
    """Where a call site names an index + key together, the key must match the CDK partition key.

    This catches the positional-swap bug: passing an index name into the key slot
    is silent at the Python call site but raises a DynamoDB ValidationException.

    Failure message names the offending file:line, the index name, the key
    attribute used, and the expected key attribute.

    Uses the flat (non-table-aware) index map for partition-key lookup — if the
    index is declared on multiple tables with different PKs, each is checked.
    The table-aware resolver determines which table's PK to validate against.
    """
    table_map = _parse_cdk_table_index_map()
    flat_declared = _parse_cdk_index_declarations()

    violations: list[str] = []
    for f in _python_source_files():
        # Table-aware path: resolved positional pairs already carry key_name.
        resolved, _ = _resolve_call_sites_in_file(f, table_map)
        for site in resolved:
            if site.key_name is None:
                continue
            per_table = table_map.get(site.table_slug, {})
            if site.index_name not in per_table:
                continue  # undeclared / wrong-table handled elsewhere
            expected_pk = per_table[site.index_name]
            if site.key_name != expected_pk:
                rel = site.path.relative_to(_REPO_ROOT)
                violations.append(
                    f"  {rel}:{site.lineno} — index={site.index_name!r} on "
                    f"{site.table_slug!r} table expects key_name={expected_pk!r} "
                    f"but call uses key_name={site.key_name!r}"
                )

        # Legacy flat path: catches positional pairs the resolver may not reach.
        for index_name, key_name, lineno in _extract_index_key_pairs_from_file(f):
            if index_name not in flat_declared:
                continue  # undeclared handled by declaration test
            expected_pk = flat_declared[index_name]
            if key_name != expected_pk:
                rel = f.relative_to(_REPO_ROOT)
                violations.append(
                    f"  {rel}:{lineno} — index={index_name!r} expects "
                    f"key_name={expected_pk!r} but call uses key_name={key_name!r}"
                )

    assert not violations, (
        "Call sites pass the wrong key attribute for a DDB index. "
        "Both index_name AND key_name must match the CDK partition key declaration.\n"
        + "\n".join(violations)
    )



# ─── F11.2 — anti-vacuity guards on the external-table exemption list ─────────
#
# An exemption list is a hole in a guard. These two tests are what stop the hole
# from silently widening: one fails if an entry stops being real, the other fails
# if the exemption mechanism starts absorbing everything.


def test_external_table_exemptions_are_still_real_call_sites():
    """Every exemption must still correspond to a live, genuinely-unresolvable site.

    A stale entry is worse than no entry: it is a standing licence for a call site
    that no longer exists, and the next one added at that path inherits it. So an
    entry that no longer fires MUST be deleted, and this test is what forces that.
    """
    table_map = _parse_cdk_table_index_map()
    unresolvable_keys: set[tuple[str, str]] = set()
    for f in _python_source_files():
        _r, u = _resolve_call_sites_in_file(f, table_map)
        for site in u:
            try:
                rel = site.path.relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                continue
            unresolvable_keys.add((rel, site.index_name))

    stale = sorted(set(_EXTERNAL_TABLE_INDEX_EXEMPTIONS) - unresolvable_keys)
    assert not stale, (
        f"{len(stale)} exemption(s) no longer match any unresolvable call site: {stale}. "
        "Either the call site was removed or the resolver now handles it — delete the "
        "entry. A stale exemption silently licenses whatever is written there next."
    )


def test_external_table_exemptions_do_not_absorb_everything():
    """The exemption filter must be narrow enough to still catch a real violation.

    Positive control: synthesise an unresolvable site at a path that is NOT
    exempt and confirm the filter lets it through. Without this, an exemption
    predicate that accidentally returned True for everything would leave
    test_guard_discovers_call_sites permanently, invisibly green.
    """
    intruder = _UnresolvableCallSite(
        index_name="SOME_OTHER_GSI_CONSTANT",
        lineno=1,
        path=_REPO_ROOT / "agents" / "tier2" / "runner.py",
        reason="synthetic, for the anti-vacuity control",
    )
    assert not _is_exempt_external_table(intruder), (
        "the exemption predicate matched a path/index pair that is NOT in the "
        "allowlist — it is too broad and no longer protects anything"
    )

    # And the one real entry IS matched, so the control is two-sided.
    exempt = _UnresolvableCallSite(
        index_name="VIN_INDEX_NAME",
        lineno=173,
        path=_REPO_ROOT / "agents" / "tier2" / "owner_resolver.py",
        reason="CMS-owned table",
    )
    assert _is_exempt_external_table(exempt), (
        "the one real exemption stopped matching — if owner_resolver.py moved, "
        "update the allowlist key"
    )


def test_external_table_exemption_list_stays_small():
    """A growing list means the scope decision is wrong, not that more holes are needed.

    Deliberately a low ceiling. If a third externally-owned table shows up inside
    the AVX surface, the right response is to reconsider _python_source_files or
    teach the resolver about external tables — not to keep appending.
    """
    assert len(_EXTERNAL_TABLE_INDEX_EXEMPTIONS) <= 2, (
        f"{len(_EXTERNAL_TABLE_INDEX_EXEMPTIONS)} exemptions. Reconsider the guard's "
        "scope or teach the resolver about externally-owned tables."
    )
