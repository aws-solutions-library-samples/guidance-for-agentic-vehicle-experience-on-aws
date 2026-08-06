# Corpus Authoring Conventions

This note is for humans authoring or reviewing corpus documents. It is **not** a corpus
document — it carries no `source_category` front matter and no `.metadata.json` sidecar,
so Bedrock does not ingest it.

---

## Two-file convention

Every corpus document is a **pair**:

| File | Purpose |
| --- | --- |
| `<NN>-<slug>.md` | Prose content with YAML front matter |
| `<NN>-<slug>.md.metadata.json` | Bedrock sidecar — filterable metadata |

**Both halves are required.** The front matter serves the local keyword-search fallback in
`retrieve.py` (the show-day safety net). The `.md.metadata.json` sidecar is what Bedrock
ingests as filterable metadata. Authoring one without the other yields a document that
either cannot be filtered in the KB or cannot be found offline.

The sidecar filename must end in `.md.metadata.json` — not `.metadata.json` alone, not
`.json`. A wrong suffix silently produces unfiltered documents.

### Front matter format

```yaml
---
source_category: <category>
stage: <camelCase stage>      # manufacturing documents only
title: <human-readable title>
---
```

### Sidecar format

```json
{"metadataAttributes": {"source_category": "<category>", "stage": "<camelCase stage>"}}
```

Omit `stage` for non-manufacturing documents (lineup narrative, owner manual, IVE package).
For IVE package documents, include `"ive_package": "<identifier>"` instead.

---

## Category names

Four categories introduced by the Zone 2 / Meridian spec:

| `source_category` value | Content | Meridian-specific? |
| --- | --- | --- |
| `vehicle_manufacturing` | One page per pipeline stage — body-in-white, robotic sub-assembly, automotive paint, final trim, quality gates, outbound logistics | **No** — generic OEM-neutral |
| `meridian_lineup_narrative` | One page per Meridian lineup entry (Windrose, Trailwind, Crestwind, Azimuth) | Yes |
| `meridian_owner_manual` | Thin ownership coverage: safety, warranty, first service, connected features | Yes |
| `meridian_ive_package` | One page per in-vehicle-experience package | Yes |

The `vehicle_shopping_guide` category contains four-wheeler-native shopping guidance (10 docs).
Two-wheeler-specific shopping and manufacturing categories were retired on 2026-08-04 when the
vehicle-type-specific corpora were collapsed into one generically-named corpus of each kind
(spec `2026-08-04-cvx-oem-discover-order-meridian`, amendment 1).

**Locale standard: American English.** Corpus prose uses `trunk`, `tire`, `gas`, `curb weight`,
`maneuver`, `center`, `liters`, `color`. Metric figures may carry a US customary equivalent in
parentheses on first use. This is a retrieval-correctness requirement, not a style preference:
the audit set and a show-floor visitor both phrase questions in American English, and a corpus
written in British English measurably failed the 0.55 retrieval threshold on 2026-08-04
(`vsg-002` 0.5401, `vsg-003` 0.5042 — both cleared after normalization, 0.6319 / 0.5780).

---

## `stage` values for `vehicle_manufacturing`

Stage values must exactly match `order_progressor.STAGES` — camelCase, not file-slug
kebab-case. The file `04-sub-assembly.md` carries `"stage": "subAssembly"` in its sidecar,
not `"sub-assembly"`.

```
orderPlaced
paymentReceived
assignedToPlant
subAssembly        ← note camelCase (file slug: 04-sub-assembly.md)
paint
finalAssembly
qualityControl
shipped
atDealer
readyForDelivery
```

The appendix documents (`A-typical-cadence.md`, `B-visibility-signals.md`) use
`"stage": "cross-cutting"` in their sidecars.

---

## IVE package identifiers for `meridian_ive_package`

The four identifiers are a cross-zone contract appearing in the order payload, the
catalog `IVE#` rows, and Zone 4 personalization. Use them exactly:

```
family
executive
adventure
entry-urban
```

Do not invent variants (`family-pkg`, `executive-package`, etc.).

---

## Depth target

Calibrated to the existing corpus (measured 2026-07-31):

| Corpus | Min words | Median words | Max words |
| --- | --- | --- | --- |
| `vehicle_manufacturing` | 510 | 591 | 837 |
| `vehicle_shopping_guide` | 1256 | 1391 | — |

For `vehicle_manufacturing` and `vehicle_shopping_guide`: **every page ≥ 450
words, category median ≥ 550**. A thin corpus retrieves badly and narrates blandly.

Documents must carry the literal terms a visitor would use. The ADP precedent is a warning:
its P0420 guide landed at a 0.5666 score against a 0.55 threshold — a 0.017 margin. Write
to be retrieved, not to be summarised.

---

## Public-mirror rules

Meridian is intended to be committable and publishable. Every corpus document must be:

- **No real OEM names.** Do not name real vehicle manufacturers, brands, or models in
  comparison or as reference. Meridian is the fictional marque.
- **No customer canaries.** No references to real customers (real fleet operators by name)
  appear in corpus prose.
- **No licensed text.** No excerpts from real owner manuals, regulatory filings, or
  standards documents. References to generic standards (ISO, SAE) by name are fine;
  verbatim text is not.
- **No imagery binaries.** Corpus files are Markdown prose only — no embedded images, no
  binary attachments.
- **Internally consistent figures.** When a number appears (range estimate, weight,
  screen size), it must match the same figure used elsewhere in the corpus. Prefer
  ranges and qualitative description over invented precision per spec R4.
- **No real-world regulatory citations in owner-manual content.** Owner-manual documents
  are demo content for a fictional marque and must not read as actual legal commitments.

When in doubt, reword rather than add a `scan_exclude` entry.

---

## Authoring checklist

Before committing a new document:

- [ ] File is `<NN>-<slug>.md` in the correct category directory
- [ ] Sidecar is `<NN>-<slug>.md.metadata.json` (suffix exact, not `.json` alone)
- [ ] Front matter has `source_category`, `title`, and `stage` (manufacturing) or
      `ive_package` (IVE package documents)
- [ ] Sidecar `metadataAttributes` matches the front matter values exactly
- [ ] `stage` value is camelCase and matches `order_progressor.STAGES` exactly (manufacturing)
- [ ] `ive_package` value is one of: `family`, `executive`, `adventure`, `entry-urban` (IVE package)
- [ ] Word count ≥ 450 for manufacturing documents (run `wc -w <file>`)
- [ ] Document contains the literal terms a visitor would use when asking about this topic
- [ ] No real OEM names, no customer canaries, no licensed text, no real regulatory citations
- [ ] Figures are internally consistent with other documents in the corpus

## Verifying a change to the corpus

Editing a document changes what the agent says, so the retrieval audit is the
check that matters — not a spell-check. After editing, re-ingest and re-run the
audit set:

```bash
# 1. Upload only what changed. NEVER `aws s3 sync --delete`: the same KB prefix
#    holds 158 two-wheeler objects across ten other categories.
#    Note the convention mismatch — S3 prefixes are HYPHENATED, source_category
#    values are UNDERSCORED. Retrieval filters on the sidecar value.
B=adp-staging-foundation-lake-$(aws sts get-caller-identity --query Account --output text)-us-east-1
P=knowledge/vehicle_knowledge_base/sources
aws s3 cp corpora/meridian_lineup_narrative/02-trailwind.md \
  "s3://$B/$P/meridian-lineup-narrative/02-trailwind.md" --region us-east-1

# 2. Re-ingest. Expect numberOfDocumentsFailed: 0 and numberOfDocumentsDeleted: 0.
aws bedrock-agent start-ingestion-job --knowledge-base-id YULHRAUMNU \
  --data-source-id QZL8HLXQ1H --region us-east-1 --description "corpus edit"

# 3. WAIT ~90s. The AOSS index lags ingestion COMPLETE by 50-90s; a retrieve
#    immediately after returns nothing, which is not a failure.

# 4. Run the gate.
VSA_ADP_KB_ID=YULHRAUMNU python3 scripts/smoke-test-kb-retrieve.py \
  --audit-set evals/kb_audit/meridian_audit_set.json --category-filter
```

Rules when a prompt scores below the 0.55 threshold:

- **Fix the document, never the threshold**, and never edit the audit set's
  `expected_doc` to match whatever came back. The threshold is the contract.
- **Where the vocabulary sits matters as much as whether it is present.** With
  512-token fixed-size chunking, query phrasing buried in a long body paragraph
  gets diluted. Concentrating it into a short dedicated section near the top moved
  one prompt's margin 7.7x (0.0019 → 0.0147) with no new facts added.
- **If several documents mention a topic once each, none of them is the answer.**
  Four IVE pages each mentioned the NFC key card in passing, so retrieval for
  handover questions picked arbitrarily among them and all scored ~0.49. The fix
  was one canonical explanation plus cross-references, not four better one-liners.
- **Run the audit unfiltered too, as a diagnostic.** It shows how the two-wheeler
  and four-wheeler corpora compete: they share all ten stage names and filenames,
  so an unfiltered manufacturing query can return the wrong vehicle class with
  high confidence.

Baseline results, including honest near-miss margins, live in
`evals/kb_audit/baselines/`. Per-tenant category configuration
(`VSA_ACQUIRE_KB_SOURCE_CATEGORY`, `VSA_DISCOVER_KB_SOURCE_CATEGORY`) is documented
in `docs/DEPLOYMENT.md` § Knowledge base.
