# Evidence Model

**Implementation:** `core/models.py` (`Evidence`, `SourceDocument`, `Source`,
`LlmExtraction`), `api/queries.py`, `api/routers/evidence.py`.

---

## 1. The chain

Every material statement in Molecule Radar resolves through this chain:

```
CLAIM            a sentence shown to a user
  └─ EVIDENCE    claim_text, extract or structured_record, confidence, dates
      └─ SOURCE DOCUMENT   locator, sha256, retrieved_at, published_at, http_status
          └─ SOURCE        publisher, licence, access mode, verification level
```

Clicking "DRL has peptide capability" shows the capability row, its assertion
level, its evidence, the exact parsed record, the file it came from with that
file's checksum and retrieval timestamp, and the publisher's own licence and
disclaimer.

## 2. Rules the schema enforces

* `evidence.source_document_id` is **NOT NULL**. An evidence row cannot exist
  without an artefact behind it.
* A CHECK constraint requires `llm_extraction_id` whenever `extraction_method` is
  `llm_extraction`. A model-derived claim that cannot be audited cannot be stored.
* `source_document` rows are immutable and unique on `(source_id, sha256)`. The
  checksum is how we know a monthly file genuinely changed.
* Fact-bearing tables (`drug_product`, `patent`, `exclusivity`,
  `drug_master_file`, `clinical_trial`, `event`, `company_capability`,
  `company_role`) carry a non-null `evidence_id`.

The data-quality suite re-checks these against stored data, because a constraint
can be bypassed by a raw load or a migration.

## 3. Extraction methods

| Method | Meaning | Audit required |
| --- | --- | --- |
| `deterministic_parse` | Parsed from structured data by code | No — the code is the audit |
| `llm_extraction` | Extracted from a difficult document by a model | **Yes** — `llm_extraction` row |
| `human_curation` | Entered by a person | Attributed to that person |

The `llm_extraction` audit record stores model, prompt version, prompt hash,
task, input reference, raw output, parsed output, confidence, token usage and
timestamp — enough to reproduce and review the extraction. It is surfaced in the
evidence drawer, not hidden.

## 4. What is structurally impossible

**A claim whose only origin is a model's recollection.** There is no way to
express it: evidence requires a source document, a source document requires a
retrieval, and a retrieval has a checksum. A model may summarise, classify or
extract from a document we hold; it cannot be the origin of a fact.

This is why `docs/DATA_SOURCE_REGISTER.md` lists "LLM knowledge as a data source"
under REJECTED. It is not a policy we remember to follow — it is a shape the
schema does not have.

## 5. Publisher caveats travel with the data

`source_document.disclaimer_text` captures the publisher's own disclaimer (for
example openFDA's statement that its data is not for clinical decision-making),
and the evidence drawer reproduces it. Reusing a publisher's data while
discarding its caveats would misrepresent that publisher.

`source.attribution_text` and `source.licence` travel the same way, so attribution
requirements are satisfiable wherever the data is shown.

## 6. Confidence on an evidence row

`evidence.confidence` is about **this extraction**, not about the opportunity:
how sure are we that we read this record correctly? A deterministic parse of a
tilde-delimited field is 1.0. An LLM extraction from a scanned PDF is not.

Opportunity-level Evidence Confidence is a different thing, computed in
`signals/scoring.py` from source breadth, volume, recency and coverage.

## 7. Deep links

`evidence.source_url` points at the publisher's own UI for the record (a
Drugs@FDA overview page, a ClinicalTrials.gov study page) so a human can verify
against the authority directly rather than trusting our copy.

## 8. Failure behaviour

If an evidence record cannot be retrieved, the API returns 404 with the message
that a claim resting on it "must not be treated as supported", and the UI renders
a BROKEN EVIDENCE LINKS banner. The product never silently drops a citation,
because a claim that quietly loses its evidence is worse than one that visibly
fails.
