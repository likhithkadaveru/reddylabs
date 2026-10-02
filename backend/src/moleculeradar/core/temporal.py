"""Temporal visibility — the leakage gate.

Every historical claim Molecule Radar could ever make to a customer depends on one
property: when the engine runs with `as_of_date = D`, it must not see a single fact
that became public after D.

That property is enforced here and nowhere else. Query builders elsewhere in the
codebase must obtain their filters from this module rather than writing date
predicates by hand, so there is exactly one place to audit and exactly one place
to test.

Two distinct notions of time matter:

* **Knowability** — could a careful analyst have known this on date D? Governed by
  `published_at` (when the publisher released it) falling back to `event_date`
  (when it happened). If a trial started on 2022-06-01 but the registry only
  posted it on 2023-02-01, an analyst on 2023-01-01 could not have known.
* **Observation** — when did *we* record it? Governed by `observed_at`. Used to
  reproduce exactly what the system itself held, which is a stricter test.

`VisibilityMode` chooses between them. The default (`KNOWABLE`) is the honest
setting for "what would a good analyst have seen"; `OBSERVED` is the stricter
"what did this system actually hold" and is what a defensible published backtest
should use once we have accumulated real observation history.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, and_, or_

if TYPE_CHECKING:  # pragma: no cover
    pass


class VisibilityMode(StrEnum):
    KNOWABLE = "knowable"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class TemporalContext:
    """The time window an engine run is permitted to see.

    `as_of_date` of None means "now": no restriction. Any non-None value puts the
    run into historical mode and every read must be gated.
    """

    as_of_date: dt.date | None = None
    mode: VisibilityMode = VisibilityMode.KNOWABLE

    @property
    def is_historical(self) -> bool:
        return self.as_of_date is not None

    @property
    def effective_date(self) -> dt.date:
        return self.as_of_date or dt.date.today()

    def describe(self) -> str:
        if not self.is_historical:
            return "live (no temporal restriction)"
        return f"as of {self.as_of_date.isoformat()} ({self.mode.value} visibility)"


# Sentinel used by callers that genuinely want everything.
LIVE = TemporalContext()


def _as_datetime_end_of_day(d: dt.date) -> dt.datetime:
    """Inclusive upper bound for a timestamp column against a date.

    A fact published at 14:00 on the as-of date was knowable on that date, so the
    bound must be the end of the day, in UTC, not midnight.
    """
    return dt.datetime.combine(d, dt.time.max, tzinfo=dt.UTC)


def visibility_clause(
    model: Any,
    ctx: TemporalContext,
    *,
    published_at_attr: str = "published_at",
    event_date_attr: str = "event_date",
    observed_at_attr: str = "observed_at",
) -> ColumnElement[bool] | None:
    """Build the SQL predicate restricting `model` to what was visible at `ctx`.

    Returns None when no restriction applies (live mode), which callers should
    treat as "add no filter".

    Rules:

    * A row with no date information at all is **excluded** from historical runs.
      We cannot prove it was knowable, so we must not use it. Being wrong in the
      permissive direction would silently invent foresight.
    * `published_at` wins over `event_date` when present, because publication is
      what made the fact knowable.
    * In OBSERVED mode, `observed_at` must additionally be on or before the bound.
      A row we had not yet recorded cannot have influenced a historical run.
    """
    if not ctx.is_historical:
        return None

    bound_date = ctx.as_of_date
    assert bound_date is not None  # guaranteed by is_historical
    bound_ts = _as_datetime_end_of_day(bound_date)

    published = getattr(model, published_at_attr, None)
    event_date = getattr(model, event_date_attr, None)
    observed = getattr(model, observed_at_attr, None)

    knowable_terms: list[ColumnElement[bool]] = []
    if published is not None and event_date is not None:
        # published_at if present, else event_date — expressed so that a NULL
        # published_at does not swallow the row.
        knowable_terms.append(
            or_(
                published <= bound_ts,
                and_(published.is_(None), event_date <= bound_date),
            )
        )
    elif published is not None:
        knowable_terms.append(published <= bound_ts)
    elif event_date is not None:
        knowable_terms.append(event_date <= bound_date)
    else:
        # The model carries no knowability columns. Refuse rather than guess:
        # a caller gating a model with no dates is a programming error we want
        # to surface loudly, not a condition to paper over.
        raise TemporalGateError(
            f"{getattr(model, '__name__', model)!r} exposes neither "
            f"{published_at_attr!r} nor {event_date_attr!r}; it cannot be "
            "temporally gated. Add the columns or read it through a gated parent."
        )

    terms = list(knowable_terms)
    if ctx.mode is VisibilityMode.OBSERVED:
        if observed is None:
            raise TemporalGateError(
                f"{getattr(model, '__name__', model)!r} has no {observed_at_attr!r} "
                "column and so cannot be queried in OBSERVED mode."
            )
        terms.append(observed <= bound_ts)

    return and_(*terms) if len(terms) > 1 else terms[0]


def is_visible(
    ctx: TemporalContext,
    *,
    published_at: dt.datetime | None = None,
    event_date: dt.date | None = None,
    observed_at: dt.datetime | None = None,
) -> bool:
    """In-Python mirror of `visibility_clause`, for already-loaded records.

    Kept deliberately close to the SQL so the two cannot drift; a test asserts
    they agree on a shared table of cases.
    """
    if not ctx.is_historical:
        return True

    bound_date = ctx.as_of_date
    assert bound_date is not None
    bound_ts = _as_datetime_end_of_day(bound_date)

    if published_at is not None:
        knowable = _coerce_utc(published_at) <= bound_ts
    elif event_date is not None:
        knowable = event_date <= bound_date
    else:
        # No date evidence → not provably knowable → excluded.
        return False

    if not knowable:
        return False

    if ctx.mode is VisibilityMode.OBSERVED:
        if observed_at is None:
            return False
        return _coerce_utc(observed_at) <= bound_ts

    return True


def _coerce_utc(value: dt.datetime) -> dt.datetime:
    """Treat a naive timestamp as UTC.

    Ingestion stores timezone-aware UTC timestamps, but fixtures and some source
    payloads carry naive ones. Comparing naive to aware raises, so normalise.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def months_between(earlier: dt.date, later: dt.date) -> int:
    """Whole months from `earlier` to `later`; negative if `later` precedes it.

    Used for exclusivity/patent runway features. Calendar months, not 30-day
    blocks, because that is how exclusivity is discussed commercially.
    """
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return months


class TemporalGateError(RuntimeError):
    """Raised when a model cannot be safely gated. Never caught to proceed anyway."""
