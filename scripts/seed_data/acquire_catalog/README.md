# Acquire Catalog Seed Data

This directory contains seed data and instructions for populating the
`vsa-acquire-catalog` DynamoDB table used by the CVX Acquire (Build &
Configure a Bike) feature.

## Schema reference

The catalog schema is defined in the CMS repo:

```
connected-mobility-guidance-on-aws/docs/acquire-catalog-schema.md
```

Section 2 of that document covers the `vsa-acquire-catalog` DynamoDB
table, including item-key patterns (`META#`, `CAT#`, `MODEL#`, `VAR#`,
`COLOR#`, `ACC#`, `ACC-COMPAT#`), required fields per item type, and
the GSI layout.

## Seeding the GenericMoto demo catalog

The `genericmoto-catalog.json` file in this directory is the
public-mirror-safe reference seed.  It uses invented model names and
placeholder image paths.

Run from the repo root:

```bash
STAGE=dev AWS_REGION=us-east-1 \
    python3 scripts/seed-acquire-catalog.py \
    --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json
```

Override the target table:

```bash
VSA_ACQUIRE_CATALOG_TABLE=my-custom-table \
    python3 scripts/seed-acquire-catalog.py \
    --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json
```

Validate without writing (dry run):

```bash
python3 scripts/seed-acquire-catalog.py \
    --catalog scripts/seed_data/acquire_catalog/genericmoto-catalog.json \
    --dry-run
```

Validate a file directly (no DynamoDB connection needed):

```bash
python3 scripts/lib/validate_acquire_catalog.py \
    scripts/seed_data/acquire_catalog/genericmoto-catalog.json
```

## Internal-tenant catalog (local-only, never committed)

Internal-tenant catalog JSON files — which contain real OEM model names,
colorway names, plant names, and other branded content — go in:

```
scripts/seed_data/acquire_catalog/<your-oem>/
```

This path is **gitignored** (see `.gitignore`).  Internal-tenant content
is never committed to the repository, not even to the internal GitLab
remote.  It lives only on the developer's local machine and is written
into their own AWS account's DynamoDB table by running the seeder script
locally.

Internal-tenant seed files follow the exact same JSON shape as
`genericmoto-catalog.json`.  To seed an internal-tenant catalog:

```bash
STAGE=dev AWS_REGION=us-east-1 \
    python3 scripts/seed-acquire-catalog.py \
    --catalog scripts/seed_data/acquire_catalog/<your-oem>/<your-oem>-catalog.json
```

Any JSON file that conforms to the schema defined in
`connected-mobility-guidance-on-aws/docs/acquire-catalog-schema.md § 2`
can be seeded with the same script — new OEM tenants are a catalog-swap,
not a code change.

## Validating a catalog before seeding

The validator exits 0 on a clean file and exits 1 with per-violation
lines on stderr:

```bash
python3 scripts/lib/validate_acquire_catalog.py \
    scripts/seed_data/acquire_catalog/genericmoto-catalog.json
```

The seeder also runs validation before writing; the `--dry-run` flag
combines validation with a count summary without touching AWS.
