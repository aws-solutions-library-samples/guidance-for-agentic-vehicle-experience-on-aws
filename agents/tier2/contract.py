"""
Tier 2 artifact contract — spec § D4.

Pure schema and hashing; no boto3, no I/O.

Public surface (pinned by agents/tier2/tests/test_contract.py):

    validate_artifact(dict) -> dict        raises ValueError on any violation
    build_pk(vin: str) -> str              returns "VEH#{vin}"
    build_sk(component, inputs_hash) -> str  raises ValueError for unknown component
    VALID_COMPONENTS  frozenset
    VALID_CONFIDENCES frozenset
    VALID_PROVENANCES frozenset

Artifact schema (M4 — all fields required unless noted):

    pk              "VEH#{vin}"
    sk              "FINDING#{component}#{inputs_hash}"  — content-addressed (M6)
    computed_at     ISO-8601 UTC string
    confidence      "high" | "medium" | "low"
    evidence        list[EvidenceEntry]  — may be empty (absent-data scenario)
    agent_version   non-empty string
    inputs_hash     SHA-256 hex string (64 chars lowercase) — idempotency key (M6)
    finding         non-empty plain-language string
    severity_hint   "routine" | "soon" | "attention"  — advisory only, never a P-level
    ttl             positive integer (epoch seconds — DynamoDB TTL attribute)

EvidenceEntry fields (all required on each entry):

    source      string
    value       any JSON-serialisable value
    provenance  "live" | "mocked" | "absent"

inputs_hash normalisation:
    To ensure the hash is stable across row ordering and float formatting, callers should
    normalise their ADP input rows before hashing:
      1. Sort rows by a stable key (e.g. VIN then timestamp).
      2. Serialise each row's numeric fields with a fixed precision (6 decimal places).
      3. Serialise to JSON with sorted keys and no extra whitespace.
      4. Hash the UTF-8 bytes of the joined JSON strings with SHA-256.
    This module validates the resulting hash string; it does not perform the normalisation
    itself — that belongs to the component data-source module that produced the rows.
"""

import re

# ---------------------------------------------------------------------------
# Vocabulary constants — the sole source of truth for valid enum values.
# ---------------------------------------------------------------------------

VALID_COMPONENTS: frozenset = frozenset({"tires", "battery", "brakes"})
"""S1 components: tires, battery, brakes — all three ADP products are now deployed (unblocked 2026-08-04)."""

VALID_CONFIDENCES: frozenset = frozenset({"high", "medium", "low"})
"""Agent-assigned confidence in the finding."""

VALID_PROVENANCES: frozenset = frozenset({"live", "mocked", "absent"})
"""Provenance of each evidence entry.  S1 uses only 'live' and 'absent',
but 'mocked' is introduced here so S6/M14 does not have to retrofit it."""

VALID_SEVERITY_HINTS: frozenset = frozenset({"routine", "soon", "attention"})
"""Canonical scheduling-urgency vocabulary.  Deliberately avoids low/medium/high/critical.
'critical' maps onto a P0 verdict, which D8 forbids this field from carrying.
P0/P1/P2/P3 are unconditionally rejected by validate_artifact()."""

_P_LEVELS: frozenset = frozenset({"P0", "P1", "P2", "P3"})
"""Triage classification vocabulary — explicitly forbidden in severity_hint."""

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
"""Pattern for a valid lowercase SHA-256 hex digest (64 characters)."""

# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def build_pk(vin: str) -> str:
    """Return the DynamoDB partition key for a vehicle.

    Format: ``VEH#{vin}``

    Parameters
    ----------
    vin:
        Vehicle Identification Number (not validated for length/format here;
        callers are responsible for passing a non-empty string).
    """
    return f"VEH#{vin}"


def build_sk(component: str, inputs_hash: str) -> str:
    """Return the DynamoDB sort key for a health finding.

    Format: ``FINDING#{component}#{inputs_hash}``

    The sort key is **content-addressed**: identical ADP inputs produce an identical
    ``inputs_hash`` and therefore an identical sort key.  This is what makes M6
    idempotency structural — a re-run over unchanged data targets the *same* item,
    so the ``attribute_not_exists(inputs_hash)`` condition on the writer finds a
    collision and no-ops.  With a ``computed_at``-keyed sort key, tomorrow's run
    would target a *different* item, the condition would trivially pass, and a
    duplicate would be written (spec § D4 — corrected 2026-08-04).

    ``computed_at`` remains a required *attribute* on the artifact so the Tier 1 tool
    can return "as of last Tuesday" (M5); it simply does not appear in the key.

    Parameters
    ----------
    component:
        Must be a member of ``VALID_COMPONENTS``.
    inputs_hash:
        SHA-256 hex digest (64 lowercase chars) of the normalised ADP input rows.

    Raises
    ------
    ValueError
        If *component* is not in ``VALID_COMPONENTS``.
    """
    if component not in VALID_COMPONENTS:
        raise ValueError(
            f"Invalid component {component!r}. "
            f"Must be one of: {sorted(VALID_COMPONENTS)}"
        )
    return f"FINDING#{component}#{inputs_hash}"


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({
    "pk",
    "sk",
    "computed_at",
    "confidence",
    "evidence",
    "agent_version",
    "inputs_hash",
    "finding",
    "severity_hint",
    "ttl",
})

_EVIDENCE_REQUIRED_FIELDS = frozenset({"source", "value", "provenance"})


def validate_artifact(artifact: dict) -> dict:
    """Validate a health-finding artifact against the M4 schema.

    Returns the artifact unchanged if valid (allows callers to use
    ``validated = validate_artifact(raw)`` inline).

    Parameters
    ----------
    artifact:
        Dict that should conform to the M4 schema described in spec § D4.

    Returns
    -------
    dict
        The same *artifact* dict, unmodified.

    Raises
    ------
    ValueError
        On any schema violation (bad field value, missing required field, etc.).
    KeyError
        Propagated naturally if a required key is absent and the caller
        used direct key access — callers may catch either ``ValueError`` or
        ``KeyError`` per the test contract.
    """
    # --- required fields present --------------------------------------------
    missing = _REQUIRED_FIELDS - set(artifact.keys())
    if missing:
        raise ValueError(f"Missing required fields: {sorted(missing)}")

    # --- pk -----------------------------------------------------------------
    pk: str = artifact["pk"]
    if not isinstance(pk, str) or not pk.startswith("VEH#"):
        raise ValueError(f"pk must start with 'VEH#', got {pk!r}")

    # --- sk -----------------------------------------------------------------
    sk: str = artifact["sk"]
    if not isinstance(sk, str) or not sk.startswith("FINDING#"):
        raise ValueError(f"sk must start with 'FINDING#', got {sk!r}")

    # sk format: FINDING#{component}#{inputs_hash}
    parts = sk.split("#", 2)  # at most 3 parts
    if len(parts) < 3:
        raise ValueError(
            f"sk must be 'FINDING#{{component}}#{{inputs_hash}}', got {sk!r}"
        )
    sk_component = parts[1]
    if sk_component not in VALID_COMPONENTS:
        raise ValueError(
            f"sk contains unknown component {sk_component!r}. "
            f"Must be one of: {sorted(VALID_COMPONENTS)}"
        )
    sk_hash = parts[2]
    if not _SHA256_RE.match(sk_hash.lower()):
        raise ValueError(
            f"sk must end with a 64-character lowercase hex SHA-256 digest "
            f"(the inputs_hash), got {sk_hash!r}. "
            f"The sort key is content-addressed: FINDING#{{component}}#{{inputs_hash}}."
        )

    # --- computed_at --------------------------------------------------------
    computed_at = artifact["computed_at"]
    if not isinstance(computed_at, str) or not computed_at.strip():
        raise ValueError("computed_at must be a non-empty string")

    # --- confidence ---------------------------------------------------------
    confidence = artifact["confidence"]
    if confidence not in VALID_CONFIDENCES:
        raise ValueError(
            f"confidence {confidence!r} is not valid. "
            f"Must be one of: {sorted(VALID_CONFIDENCES)}"
        )

    # --- evidence -----------------------------------------------------------
    evidence = artifact["evidence"]
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    for i, entry in enumerate(evidence):
        missing_ev = _EVIDENCE_REQUIRED_FIELDS - set(entry.keys())
        if missing_ev:
            raise ValueError(
                f"evidence[{i}] missing fields: {sorted(missing_ev)}"
            )
        provenance = entry["provenance"]
        if provenance not in VALID_PROVENANCES:
            raise ValueError(
                f"evidence[{i}].provenance {provenance!r} is not valid. "
                f"Must be one of: {sorted(VALID_PROVENANCES)}"
            )

    # --- inputs_hash --------------------------------------------------------
    inputs_hash = artifact["inputs_hash"]
    if not isinstance(inputs_hash, str):
        raise ValueError("inputs_hash must be a string")
    if not _SHA256_RE.match(inputs_hash.lower()):
        raise ValueError(
            f"inputs_hash must be a 64-character lowercase hex SHA-256 digest, "
            f"got {inputs_hash!r} (length {len(inputs_hash)})"
        )

    # --- ttl ----------------------------------------------------------------
    ttl = artifact["ttl"]
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise ValueError("ttl must be an integer")
    if ttl <= 0:
        raise ValueError(f"ttl must be a positive integer, got {ttl}")

    # --- finding ------------------------------------------------------------
    finding = artifact["finding"]
    if not isinstance(finding, str) or not finding.strip():
        raise ValueError("finding must be a non-empty string")

    # --- severity_hint ------------------------------------------------------
    severity_hint = artifact["severity_hint"]
    # Reject P-levels first for a clear error message.
    if severity_hint in _P_LEVELS:
        raise ValueError(
            f"severity_hint must not be a triage P-level; "
            f"got {severity_hint!r}. Use the scheduling-urgency vocabulary: "
            f"{sorted(VALID_SEVERITY_HINTS)}"
        )
    if severity_hint not in VALID_SEVERITY_HINTS:
        raise ValueError(
            f"severity_hint {severity_hint!r} is not valid. "
            f"Must be one of: {sorted(VALID_SEVERITY_HINTS)}"
        )

    # --- agent_version ------------------------------------------------------
    agent_version = artifact["agent_version"]
    if not isinstance(agent_version, str) or not agent_version.strip():
        raise ValueError("agent_version must be a non-empty string")

    return artifact
