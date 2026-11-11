"""Per-repo scan_exclude baseline for the publish-safety ratchet test.

THIS FILE IS DATA, NOT CODE.

  - It is deliberately NOT vendored from ~/.kiro/publish-toolkit/ and is therefore
    EXEMPT from the toolkit drift check (verify-sync.sh).
  - It is consumed by the vendored canonical test
    scripts/lib/test_publish_scan_exclude_invariant.py via _consumer_baseline().
  - Entries are a RATCHET: they should only ever be REMOVED as files get cleaned up
    or publish-excluded. Never add an entry without justification — doing so silently
    widens the surface of published-but-unscanned content.

Each entry below is a scan_exclude pattern that shadows at least one file that ships
to the public mirror. The content of those files is verified clean by the invariant
test above this ratchet; the ratchet's job is to ensure the set cannot grow without
a human decision recorded here.

Origin: extracted 2026-08-04 from the hardcoded `accepted` set in
scripts/tests/test_publish_scan_exclude_invariant.py (CVX repo variant) before the
canonical v0.3.0 was synced over it. Do not invent entries — only extract from the
authoritative source.
"""

BASELINE: set = {
    # Meta-files whose CONTENT is necessarily pattern-like: they contain the
    # patterns, the path fragments, or the scanner itself.
    ".publish-secrets-scan.yml",
    ".publish-exclude",
    "scripts/lib/secret-scan.py",
    "scripts/lib/test_secret_scan.py",
    # Required public-facing files carrying the AWS open-source contact
    # addresses, which trip the (warning-severity) internal_email pattern.
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    # Golden fixtures and eval cases contain persona/customer-shaped sample
    # data that trips canary strings by construction. Verified free of
    # CRITICAL findings by the test above.
    "agents/supervisor/tests/fixtures/**",
    "evals/cases/**",
    # Seed script for a named-customer demo dataset.
    "scripts/seed-meridian-fleet.py",
}
