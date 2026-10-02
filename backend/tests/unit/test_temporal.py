"""Temporal gate tests.

Leakage is the failure mode that would invalidate every historical claim the
product could make, so these tests are deliberately pedantic.
"""

from __future__ import annotations

import datetime as dt

import pytest

from moleculeradar.core.temporal import (
    LIVE,
    TemporalContext,
    TemporalGateError,
    VisibilityMode,
    is_visible,
    months_between,
    visibility_clause,
)

UTC = dt.UTC
AS_OF = dt.date(2023, 1, 1)
KNOWABLE = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.KNOWABLE)
OBSERVED = TemporalContext(as_of_date=AS_OF, mode=VisibilityMode.OBSERVED)


class TestKnowableVisibility:
    def test_published_before_as_of_is_visible(self) -> None:
        assert is_visible(KNOWABLE, published_at=dt.datetime(2022, 12, 31, tzinfo=UTC))

    def test_published_after_as_of_is_hidden(self) -> None:
        assert not is_visible(KNOWABLE, published_at=dt.datetime(2023, 1, 2, tzinfo=UTC))

    def test_published_same_day_late_is_visible(self) -> None:
        """A fact published at 14:00 on the as-of date was knowable that day."""
        assert is_visible(KNOWABLE, published_at=dt.datetime(2023, 1, 1, 14, 0, tzinfo=UTC))

    def test_publication_governs_not_event_date(self) -> None:
        """The classic leak: an old event only published later.

        A trial that started in June 2022 but was first posted in February 2023
        was NOT knowable on 2023-01-01, and using it would fabricate foresight.
        """
        assert not is_visible(
            KNOWABLE,
            published_at=dt.datetime(2023, 2, 1, tzinfo=UTC),
            event_date=dt.date(2022, 6, 1),
        )

    def test_event_date_used_when_publication_unknown(self) -> None:
        assert is_visible(KNOWABLE, event_date=dt.date(2022, 6, 1))
        assert not is_visible(KNOWABLE, event_date=dt.date(2023, 6, 1))

    def test_undated_row_is_excluded(self) -> None:
        """No date evidence means not provably knowable, so excluded.

        Erring permissive here would silently invent foresight, which is worse
        than losing a row from a historical run.
        """
        assert not is_visible(KNOWABLE)

    def test_live_context_shows_everything(self) -> None:
        assert is_visible(LIVE, published_at=dt.datetime(2099, 1, 1, tzinfo=UTC))
        assert is_visible(LIVE)

    def test_naive_timestamp_treated_as_utc(self) -> None:
        assert is_visible(KNOWABLE, published_at=dt.datetime(2022, 12, 31))


class TestObservedVisibility:
    def test_requires_observation_on_or_before_bound(self) -> None:
        assert is_visible(
            OBSERVED,
            published_at=dt.datetime(2022, 6, 1, tzinfo=UTC),
            observed_at=dt.datetime(2022, 7, 1, tzinfo=UTC),
        )

    def test_unobserved_row_hidden_even_if_published(self) -> None:
        """We cannot claim the system acted on data it had not yet recorded."""
        assert not is_visible(
            OBSERVED,
            published_at=dt.datetime(2022, 6, 1, tzinfo=UTC),
            observed_at=dt.datetime(2024, 1, 1, tzinfo=UTC),
        )

    def test_missing_observation_hidden(self) -> None:
        assert not is_visible(OBSERVED, published_at=dt.datetime(2022, 6, 1, tzinfo=UTC))


class TestSqlAndPythonAgree:
    """The SQL clause and the Python mirror must not drift apart."""

    def test_clause_is_none_in_live_mode(self) -> None:
        from moleculeradar.core.models import Event

        assert visibility_clause(Event, LIVE) is None

    def test_clause_built_for_historical_mode(self) -> None:
        from moleculeradar.core.models import Event

        clause = visibility_clause(Event, KNOWABLE)
        assert clause is not None
        sql = str(clause)
        assert "published_at" in sql
        assert "event_date" in sql

    def test_observed_mode_adds_observation_term(self) -> None:
        from moleculeradar.core.models import Event

        sql = str(visibility_clause(Event, OBSERVED))
        assert "observed_at" in sql

    def test_ungateable_model_raises(self) -> None:
        """A model with no date columns must fail loudly, not silently pass through."""

        class NoDates:
            __name__ = "NoDates"

        with pytest.raises(TemporalGateError):
            visibility_clause(NoDates, KNOWABLE)


class TestMonthsBetween:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (dt.date(2023, 1, 1), dt.date(2024, 1, 1), 12),
            (dt.date(2023, 1, 1), dt.date(2023, 7, 1), 6),
            (dt.date(2023, 1, 15), dt.date(2023, 7, 1), 5),
            (dt.date(2023, 1, 1), dt.date(2023, 1, 31), 0),
            (dt.date(2024, 1, 1), dt.date(2023, 1, 1), -12),
            (dt.date(2023, 1, 31), dt.date(2025, 7, 15), 29),
        ],
    )
    def test_calendar_months(self, a: dt.date, b: dt.date, expected: int) -> None:
        assert months_between(a, b) == expected
