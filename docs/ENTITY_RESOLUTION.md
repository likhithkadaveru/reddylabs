# Entity Resolution

**Implementation:** `resolution/normalize.py`, `resolution/engine.py`,
`resolution/focus.py`
**Tests:** `tests/unit/test_normalize.py`, `tests/integration/test_entity_resolution.py`

---

## 1. Why this is a first-class subsystem

Pharmaceutical data has no shared identifier discipline. The same company appears
as `Dr. Reddy's Laboratories Ltd`, `Dr Reddys Laboratories Limited`,
`DR. REDDYS LABORATORIES INC`, `Dr.Reddy's Laboratories Ltd` and `DRL`. The same
molecule appears as `Metformin Hydrochloride`, `metformin HCl` and
`METFORMIN HYDROCHLORIDE ER`.

Get this wrong in either direction and the product is worthless:

* **Under-merge** and a company's filings split across duplicates, understating
  its activity and potentially hiding the focus company's own capability.
* **Over-merge** and you attribute one firm's capabilities to another — a false
  claim about a real company, which is the single worst failure available to us.

Over-merging is the more damaging error, so the system is deliberately biased
towards refusing.

## 2. The ladder

Resolution attempts these in order; the first hit wins.

| Step | Method | Score | Auto-merge? |
| --- | --- | --- | --- |
| 1 | **Deterministic identifier** — shared UNII, RxCUI, DMF number, application number, NCT ID | 1.00 | Yes, conclusive |
| 2 | **Alias table** — a literal already resolved and recorded | 0.99 | Yes |
| 3 | **Normalized exact** — identical after normalisation | 0.97 | Yes |
| 4 | **Fuzzy** — token-set similarity over normalised forms | capped < 0.95 | **Never alone** |
| 5 | **Contextual** — shared country raises a fuzzy candidate | +0.03 | Only above threshold |
| 6 | **LLM-assisted** | recommendation only | **Never** |

Then:

* `>= 0.95` → link, and write an `EntityMergeLog` row stating method, score and
  rationale.
* `0.80 – 0.95` → **review queue**, and the caller receives a *new provisional
  entity* rather than a guess.
* `< 0.80` → new entity.

### Fuzzy can never auto-merge

`_best_fuzzy_candidate` caps its output at `auto_accept - 0.01`. Name similarity
alone, however strong, cannot fuse two pharmaceutical companies. Only
corroboration (a shared identifier, or contextual agreement) can lift a candidate
over the line. This is asserted directly in tests.

## 3. Normalisation

Two normalisers, because companies and molecules fail differently.

**Companies** — fold accents and typographic apostrophes (the curly `'` in
"Dr. Reddy's" defeats exact matching on its own), collapse possessives, strip
legal-form suffixes repeatedly (`Pharma Inc Ltd` chains are common), then strip
industry noise words.

```
Dr. Reddy's Laboratories Ltd      -> dr reddys
Dr Reddys Laboratories Limited    -> dr reddys
DR. REDDYS LABORATORIES INC       -> dr reddys
Ltd.                              -> ""     (refused — see below)
```

**Molecules** — strip dosage-form and release descriptors, then salts, esters and
hydrates, to reach the active moiety.

```
Metformin Hydrochloride ER        -> metformin
metformin HCl                     -> metformin
Hydrochloride                     -> ""     (refused)
```

### Empty normalisation is a refusal, not a match

A literal that normalises to an empty string (`"Ltd."`, `"Hydrochloride"`) is
**unresolvable**, not a match to every other empty string. Without that guard,
every noise-only literal would collapse into a single meaningless entity.

### Stereochemistry is preserved

`esomeprazole` does not normalise to `omeprazole`, nor `dexlansoprazole` to
`lansoprazole`. These are separate products with separate exclusivity, separate
suppliers and separate market dynamics. Merging them would attach the wrong dates
and the wrong competitive field to both. The fuzzy scorer additionally penalises
pairs differing only by a known stereochemistry prefix so they land in review
rather than merging.

### Combination splitting is case-insensitive

Orange Book serves ingredient strings entirely in upper case. A case-sensitive
`" and "` split silently failed to split every combination product — a bug found
by the first normalisation test.

## 4. Blocking

Fuzzy comparison is limited to a candidate block. Company blocking keys on the
**first token** of the normalised name, not the first N characters of the whole
string: normalisation strips a variable number of noise words, so a whole-string
prefix puts `zeta` and `zeta therapeutic holding` in different blocks and the
near-duplicate is never compared. Keying on the first token puts both in `zeta`.

## 5. Aliases

`entity_alias` records every literal the world uses, never deleted, with the
method and confidence that recorded it.

**One alias maps to at most one entity per type**, enforced by a unique
constraint. Without it two companies could both claim `drl` and resolution would
be non-deterministic — the same source literal resolving differently on different
runs. When a resolution would claim an alias another entity already holds, the
system creates a `conflicting_alias` review item rather than overwriting or
crashing.

Abbreviations must be seeded. `DRL` has no textual relationship to the full name,
so the resolver will not invent it; `focus bootstrap` records the configured seeds
as human-attributed decisions.

## 6. Every merge is explainable

`entity_merge_log` records entity type, surviving ID, merged ID, input literal,
method, score, rationale, features and who decided. No merge path skips it.

## 7. The review queue

Three kinds of item reach it:

* `company_merge_candidate` / `molecule_merge_candidate` — a fuzzy score in the
  review band. A provisional entity was created so ingestion continued; a human
  decides whether to link.
* `conflicting_alias` — a literal that would point at two entities.
* `focus_group_candidate` — a company sharing some but not all identity tokens
  with the focus company. Not linked, because a wrongly attributed subsidiary
  would credit the focus company with another firm's capabilities.

A pending review item for a molecule's linked companies lowers that molecule's
Evidence Confidence. If we are not sure the entities are right, we should not be
confident in conclusions drawn from them.

## 8. What is not built

Accepting a review item records an alias and a merge-log row, but does not
repoint rows already referencing the provisional entity. Merging those rows is a
separate operation that does not exist yet (see `KNOWN_LIMITATIONS.md` §4.4).
