"""Normalisation tests.

These encode the specific naming failures the brief named, plus the ones that
would cause damage if we got them wrong (stereoisomers, combinations).
"""

from __future__ import annotations

import pytest

from moleculeradar.resolution.normalize import (
    company_core_tokens,
    molecule_blocking_key,
    normalize_company_name,
    normalize_molecule_name,
    split_combination_ingredients,
)


class TestCompanyNormalisation:
    @pytest.mark.parametrize(
        "variant",
        [
            "Dr. Reddy's Laboratories",
            "Dr Reddys Laboratories Ltd",
            "Dr. Reddy's Laboratories Ltd",
            "Dr. Reddy's Laboratories Limited",
            "Dr Reddy's Laboratories Inc",
            "Dr. Reddy's Laboratories, Inc.",
            "Dr.Reddy's Laboratories Ltd",
            "DR. REDDYS LABORATORIES LTD",
            "Dr. Reddy's",
            "Dr Reddys",
            # Curly apostrophe, as served by several publishers.
            "Dr. Reddy’s Laboratories Ltd",
        ],
    )
    def test_drl_variants_collapse_to_one_form(self, variant: str) -> None:
        assert normalize_company_name(variant) == "dr reddys"

    def test_abbreviation_does_not_collapse_by_normalisation(self) -> None:
        """DRL must NOT normalise to the same string as the full name.

        An abbreviation has no textual relationship to the full name, so claiming
        a match would be unjustified. It must be handled by the alias table, which
        records a human or configured decision.
        """
        assert normalize_company_name("DRL") != normalize_company_name(
            "Dr. Reddy's Laboratories Ltd"
        )

    def test_distinct_companies_do_not_collapse(self) -> None:
        assert normalize_company_name("Sun Pharmaceutical Industries Ltd") != \
            normalize_company_name("Aurobindo Pharma Ltd")

    def test_pure_noise_normalises_to_empty(self) -> None:
        """A literal of only legal-form tokens must yield empty, not a match key.

        Otherwise every such literal would collapse together into one bogus company.
        """
        assert normalize_company_name("Ltd.") == ""
        assert normalize_company_name("Pharmaceuticals Inc") == ""

    def test_ampersand_handled(self) -> None:
        assert "and" in normalize_company_name("Smith & Nephew Pharma")

    def test_core_tokens_exclude_single_characters(self) -> None:
        tokens = company_core_tokens("Dr. Reddy's Laboratories Ltd")
        assert tokens == frozenset({"dr", "reddys"})


class TestMoleculeNormalisation:
    @pytest.mark.parametrize(
        "variant",
        [
            "Metformin Hydrochloride",
            "metformin HCl",
            "METFORMIN HYDROCHLORIDE ER",
            "Metformin Hydrochloride Extended Release Tablets",
            "metformin",
        ],
    )
    def test_salt_and_dosage_forms_collapse_to_moiety(self, variant: str) -> None:
        assert normalize_molecule_name(variant) == "metformin"

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Esomeprazole Magnesium", "Omeprazole"),
            ("Dexlansoprazole", "Lansoprazole"),
            ("Levocetirizine Dihydrochloride", "Cetirizine Hydrochloride"),
        ],
    )
    def test_stereoisomers_stay_distinct(self, a: str, b: str) -> None:
        """Stereoisomers are separate products with separate exclusivity.

        Merging them would be a material factual error, so normalisation must keep
        them apart even though the strings are similar.
        """
        assert normalize_molecule_name(a) != normalize_molecule_name(b)

    def test_salt_only_literal_normalises_to_empty(self) -> None:
        assert normalize_molecule_name("Hydrochloride") == ""
        assert normalize_molecule_name("Sodium") == ""

    def test_blocking_key_groups_variants(self) -> None:
        assert molecule_blocking_key("Metformin Hydrochloride") == \
            molecule_blocking_key("metformin HCl")


class TestCombinationSplitting:
    @pytest.mark.parametrize(
        ("text", "expected_count"),
        [
            ("AMOXICILLIN; CLAVULANATE POTASSIUM", 2),
            # Upper-case 'AND' is what Orange Book actually serves; a
            # case-sensitive split silently failed here before.
            ("SITAGLIPTIN AND METFORMIN HYDROCHLORIDE", 2),
            ("Sitagliptin and Metformin HCl", 2),
            ("LOPINAVIR/RITONAVIR", 2),
            ("EFAVIRENZ; EMTRICITABINE; TENOFOVIR DISOPROXIL FUMARATE", 3),
            ("METFORMIN HYDROCHLORIDE", 1),
        ],
    )
    def test_splits_combinations(self, text: str, expected_count: int) -> None:
        assert len(split_combination_ingredients(text)) == expected_count
