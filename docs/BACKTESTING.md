# Backtesting and Leakage Safety

**Implementation:** `core/temporal.py`
**Tests:** `tests/unit/test_temporal.py`, `TestTemporalLeakage` in
`tests/integration/test_opportunity_engine.py` and `test_end_to_end_pipeline.py`

---

## 1. Why this is designed in from the start

Eventually we want to say: *"Running only on information available on
2023-01-01, Molecule Radar would have surfaced these molecules — here is what
subsequently happened."*

That claim is worth a great deal and is destroyed entirely by a single leak. A
backtest that accidentally saw one future fact is not slightly optimistic; it is
evidence of nothing. So leakage safety is a property of one module, enforced in
one place, and tested directly.

## 2. Two notions of time

| Mode | Question | Governed by |
| --- | --- | --- |
| `KNOWABLE` (default) | Could a careful analyst have known this on date D? | `published_at`, falling back to `event_date` |
| `OBSERVED` (stricter) | Did *this system* actually hold it on date D? | additionally `observed_at` |

`KNOWABLE` is the honest setting for "what a good analyst could have seen".
`OBSERVED` is what a publishable backtest should eventually use, and it requires
accumulated observation history we do not yet have.

## 3. The rules

```python
TemporalContext(as_of_date=date(2023, 1, 1), mode=VisibilityMode.KNOWABLE)
```

1. **Publication governs, not occurrence.** A trial that started 2022-06-01 but
   was first posted 2023-02-01 was *not* knowable on 2023-01-01. Using it would
   fabricate foresight. This is the classic leak and it has its own test.
2. **An undated row is excluded** from historical runs. We cannot prove it was
   knowable, so we must not use it. Being permissive here would silently invent
   foresight; losing a row is the cheaper error.
3. **End-of-day bound.** A fact published at 14:00 on the as-of date was knowable
   that day.
4. **A model with no date columns cannot be gated** and raises `TemporalGateError`
   rather than silently passing through.

## 4. Where dating decisions were made

These matter more than the gate itself, because the gate can only enforce what
ingestion recorded:

| Record | Dated by | Why |
| --- | --- | --- |
| DMF | publication of the quarterly list | FDA does not publish submissions in real time. Dating at `submission_date` would claim we knew in 2015 something first published in 2026. |
| Trial | `studyFirstPostDate` | Registration is what made it public; `startDate` is kept but does not gate. |
| Patent / exclusivity listing | publication of the Orange Book file | Dating at expiry would place events years in the future. |
| Approval / filing | `submission_status_date` | FDA publishes these as dated actions. |
| Shortage | initial posting, or update date for a resolution | A resolution happened when it was reported resolved. |

## 5. Running a historical evaluation

```bash
moleculeradar engine run --as-of 2023-01-01
moleculeradar engine run --as-of 2023-01-01 --observed   # stricter
```

Results are stored against `(molecule, as_of_date, engine_version)` so runs at
different dates coexist and can be compared.

## 6. What is not yet possible

* **OBSERVED mode is not yet meaningful** — it needs accumulated observation
  history.
* **Snapshot sources limit historical reach** — Orange Book and the DMF list are
  current-state files, so a `KNOWABLE` run before our first ingestion sees
  approximately nothing from them. Meaningful historical evaluation needs either
  archived editions or months of our own snapshots.
* **There are no outcome labels**, so there is nothing to score a backtest
  against. Phase 6 manual validation comes first.

This is why the honest current claim is "designed to be leakage-safe and tested
for it", not "backtested".

## 7. How we would evaluate, once possible

1. Pick as-of dates at least 24 months in the past.
2. Run in `OBSERVED` mode.
3. Record the opportunity set.
4. Compare against subsequent observable developments: did ANDA filings appear?
   did API suppliers enter? did a shortage occur?
5. Report precision at k, evidence completeness, and the false-positive rate,
   alongside a base rate — a signal that fires on everything predicts nothing.
6. Only then revisit signal weights.
