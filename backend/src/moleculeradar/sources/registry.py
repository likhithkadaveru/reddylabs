"""Connector registry.

One place that knows every connector, so the CLI, the verification command and the
Data Health screen all agree on what exists.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from .base import SourceConnector, SourceSpec
from .clinicaltrials import SPEC as CTGOV_SPEC
from .clinicaltrials import ClinicalTrialsConnector
from .fda_dmf import SPEC as DMF_SPEC
from .fda_dmf import FdaDmfConnector
from .openfda import (
    DRUGSFDA_SPEC,
    SHORTAGES_SPEC,
    DrugsAtFdaConnector,
    DrugShortagesConnector,
)
from .orange_book import SPEC as ORANGE_BOOK_SPEC
from .orange_book import OrangeBookConnector

ConnectorFactory = Callable[[Session], SourceConnector]

# Ordered deliberately. Orange Book and the DMF list run first because they
# establish molecules and companies with the strongest identity evidence; the
# API sources then resolve against entities that already exist, which produces
# far fewer ambiguous matches than the reverse order.
REGISTRY: dict[str, tuple[SourceSpec, ConnectorFactory]] = {
    ORANGE_BOOK_SPEC.key: (ORANGE_BOOK_SPEC, OrangeBookConnector),
    DMF_SPEC.key: (DMF_SPEC, FdaDmfConnector),
    DRUGSFDA_SPEC.key: (DRUGSFDA_SPEC, DrugsAtFdaConnector),
    SHORTAGES_SPEC.key: (SHORTAGES_SPEC, DrugShortagesConnector),
    CTGOV_SPEC.key: (CTGOV_SPEC, ClinicalTrialsConnector),
}

INGESTION_ORDER: tuple[str, ...] = tuple(REGISTRY)


def get_spec(key: str) -> SourceSpec:
    if key not in REGISTRY:
        raise KeyError(
            f"Unknown source {key!r}. Known sources: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[key][0]


def build_connector(key: str, session: Session, **kwargs: Any) -> SourceConnector:
    _spec, factory = REGISTRY[get_spec(key).key]
    return factory(session, **kwargs)


def all_specs() -> list[SourceSpec]:
    return [spec for spec, _ in REGISTRY.values()]


def required_hosts() -> dict[str, tuple[str, ...]]:
    """Every host ingestion needs, by source key.

    Printed by `moleculeradar sources verify` so an operator can see exactly what
    an egress policy has to allow.
    """
    return {spec.key: spec.required_hosts for spec in all_specs()}


# Representative probe URLs for reachability checks. Chosen to be cheap: a
# one-record query or a page fetch, never a bulk download.
PROBE_URLS: dict[str, str] = {
    ORANGE_BOOK_SPEC.key: "https://www.fda.gov/robots.txt",
    DMF_SPEC.key: "https://www.fda.gov/robots.txt",
    DRUGSFDA_SPEC.key: "https://api.fda.gov/drug/drugsfda.json?limit=1",
    SHORTAGES_SPEC.key: "https://api.fda.gov/drug/shortages.json?limit=1",
    CTGOV_SPEC.key: "https://clinicaltrials.gov/api/v2/studies?pageSize=1",
}
