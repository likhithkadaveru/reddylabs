"""Normalisation of pharmaceutical names.

Entity resolution lives or dies on this module. Everything downstream — alias
lookup, blocking keys, fuzzy candidate generation — operates on normalised
strings, so a bug here propagates into every merge decision.

Two separate normalisers, because companies and molecules fail differently:

* **Companies** fail on legal suffixes, punctuation, and transliteration of
  possessives: `Dr. Reddy's Laboratories Ltd` vs `Dr Reddys Laboratories Limited`.
* **Molecules** fail on salt/ester forms, stereochemistry prefixes, and
  dosage-form contamination: `Metformin Hydrochloride ER` vs `metformin HCl`.

Deliberately conservative: a normaliser that strips too much creates false
merges, which are far more damaging than missed ones. We would rather send an
ambiguous pair to human review than quietly fuse two companies.
"""

from __future__ import annotations

import re
import unicodedata

# Legal-form suffixes stripped from company names. Order matters: longer forms
# first so "private limited" is removed before "limited".
_COMPANY_SUFFIXES: tuple[str, ...] = (
    "private limited",
    "pvt limited",
    "pvt ltd",
    "private ltd",
    "public limited company",
    "limited liability company",
    "incorporated",
    "corporation",
    "company",
    "holdings",
    "limited",
    "gmbh and co kg",
    "gmbh",
    "aktiengesellschaft",
    "ag",
    "nv",
    "bv",
    "sa",
    "sas",
    "spa",
    "srl",
    "ab",
    "as",
    "oy",
    "plc",
    "llc",
    "lllp",
    "llp",
    "lp",
    "ltd",
    "inc",
    "corp",
    "co",
    "kk",
    "kgaa",
    "kg",
    "sarl",
    "pte",
    "pty",
    "ulc",
)

# Noise tokens that appear in regulatory applicant strings but carry no identity.
_COMPANY_NOISE: tuple[str, ...] = (
    "pharmaceuticals",
    "pharmaceutical",
    "pharma",
    "laboratories",
    "laboratory",
    "labs",
    "healthcare",
    "health care",
    "lifesciences",
    "life sciences",
    "biosciences",
    "bioscience",
    "sciences",
    "science",
    "therapeutics",
    "usa",
    "us",
    "india",
    "international",
    "group",
    "worldwide",
    "global",
    "division",
    "subsidiary",
    "the",
)

# Salt, ester, hydrate and counter-ion forms. Removing these maps a form to its
# parent moiety, which is the right unit for competition analysis.
_SALT_FORMS: tuple[str, ...] = (
    "hydrochloride",
    "hydrobromide",
    "hydroiodide",
    "hcl",
    "hbr",
    "dihydrochloride",
    "sulfate",
    "sulphate",
    "bisulfate",
    "hemisulfate",
    "phosphate",
    "diphosphate",
    "hydrogen phosphate",
    "nitrate",
    "acetate",
    "diacetate",
    "trifluoroacetate",
    "maleate",
    "fumarate",
    "hemifumarate",
    "succinate",
    "tartrate",
    "bitartrate",
    "citrate",
    "dicitrate",
    "malate",
    "mesylate",
    "methanesulfonate",
    "besylate",
    "benzenesulfonate",
    "tosylate",
    "esylate",
    "napsylate",
    "pamoate",
    "palmitate",
    "stearate",
    "oleate",
    "laurate",
    "decanoate",
    "enanthate",
    "cypionate",
    "propionate",
    "valerate",
    "dipropionate",
    "furoate",
    "xinafoate",
    "lactate",
    "gluconate",
    "glucuronate",
    "oxalate",
    "pyruvate",
    "salicylate",
    "benzoate",
    "carbonate",
    "bicarbonate",
    "chloride",
    "bromide",
    "iodide",
    "fluoride",
    "sodium",
    "disodium",
    "potassium",
    "calcium",
    "magnesium",
    "zinc",
    "aluminium",
    "aluminum",
    "lithium",
    "meglumine",
    "tromethamine",
    "trometamol",
    "arginine",
    "lysine",
    "choline",
    "diolamine",
    "olamine",
    "etabonate",
    "axetil",
    "proxetil",
    "pivoxil",
    "medoxomil",
    "cilexetil",
    "fosamil",
    "monohydrate",
    "dihydrate",
    "trihydrate",
    "sesquihydrate",
    "hemihydrate",
    "anhydrous",
    "anhydrate",
)

# Dosage form / release descriptors that contaminate ingredient strings.
_DOSAGE_NOISE: tuple[str, ...] = (
    "extended release",
    "delayed release",
    "immediate release",
    "modified release",
    "sustained release",
    "controlled release",
    "prolonged release",
    "film coated",
    "enteric coated",
    "oral solution",
    "oral suspension",
    "for injection",
    "injection",
    "injectable",
    "tablets",
    "tablet",
    "capsules",
    "capsule",
    "softgel",
    "ointment",
    "cream",
    "gel",
    "solution",
    "suspension",
    "syrup",
    "powder",
    "lyophilized",
    "sterile",
    "usp",
    "ep",
    "bp",
    "micronized",
    "er",
    "xr",
    "sr",
    "dr",
    "cr",
    "la",
    "xl",
    "odt",
)

# Stereochemistry and isomer prefixes. Stripped for *blocking* only — never used
# to assert that two differently-stereo molecules are identical, because that is
# chemically false and commercially material (e.g. esomeprazole vs omeprazole).
_STEREO_PREFIXES: tuple[str, ...] = (
    "levo",
    "dextro",
    "racemic",
    "rac",
    "dl",
    "l",
    "d",
    "r",
    "s",
    "es",
)

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
# Case-insensitive: source ingredient strings are frequently upper-case
# (Orange Book products.txt is entirely upper-case), so a case-sensitive
# " and " would silently fail to split every combination product.
_COMBINATION_SPLIT = re.compile(r";|\s+and\s+|/|\+", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    """Fold accents and typographic punctuation to ASCII.

    Handles the curly apostrophe in "Dr. Reddy's" that otherwise defeats exact
    matching against sources that use a straight quote.
    """
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _base_clean(text: str) -> str:
    text = _strip_accents(text).lower()
    # Possessives collapse before punctuation removal so "reddy's" -> "reddys"
    # rather than "reddy s", which would otherwise break token matching.
    text = text.replace("'s ", "s ").replace("'s", "s").replace("'", "")
    text = text.replace("&", " and ")
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _remove_tokens(text: str, phrases: tuple[str, ...]) -> str:
    """Remove whole-word phrases, longest first, leaving token boundaries intact."""
    for phrase in sorted(phrases, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text)
    return _WS.sub(" ", text).strip()


def normalize_company_name(name: str) -> str:
    """Aggressive normalisation for company *blocking* and alias lookup.

    Returns a string suitable for exact-match lookup. Not reversible and not for
    display. An empty result means the input was entirely noise, which callers
    must treat as unresolvable rather than as a match to another empty string.

    >>> normalize_company_name("Dr. Reddy's Laboratories Ltd")
    'dr reddys'
    >>> normalize_company_name("Dr Reddys Laboratories Limited")
    'dr reddys'
    """
    text = _base_clean(name)
    # Repeat suffix stripping: "Pharma Inc Ltd" style chains are common.
    for _ in range(3):
        before = text
        text = _remove_tokens(text, _COMPANY_SUFFIXES)
        if text == before:
            break
    text = _remove_tokens(text, _COMPANY_NOISE)
    return text


def company_core_tokens(name: str) -> frozenset[str]:
    """Identity-bearing tokens of a company name, for overlap scoring."""
    normalized = normalize_company_name(name)
    return frozenset(t for t in normalized.split() if len(t) > 1)


def normalize_molecule_name(name: str) -> str:
    """Normalise a molecule name to its active-moiety form.

    Strips salts, hydrates and dosage-form noise. Stereochemistry is **kept**:
    esomeprazole and omeprazole are different products with different
    exclusivity, and merging them would be a serious factual error.

    >>> normalize_molecule_name("Metformin Hydrochloride ER")
    'metformin'
    >>> normalize_molecule_name("metformin HCl")
    'metformin'
    """
    text = _base_clean(name)
    text = _remove_tokens(text, _DOSAGE_NOISE)
    for _ in range(3):
        before = text
        text = _remove_tokens(text, _SALT_FORMS)
        if text == before:
            break
    return text


def split_combination_ingredients(ingredient_text: str) -> list[str]:
    """Split a combination-product ingredient string into components.

    Orange Book uses '; ' for combinations; other sources use ' and ' or '/'.
    Combination products are a distinct commercial unit from their components,
    so callers decide whether to treat the combination or its parts as the
    molecule — this function only exposes the parts.
    """
    parts = _COMBINATION_SPLIT.split(ingredient_text)
    return [p.strip() for p in parts if p.strip()]


def molecule_blocking_key(name: str) -> str:
    """Coarse key used to limit fuzzy comparison to plausible candidates.

    First six characters of the normalised moiety. Crude but effective: it keeps
    candidate sets small without discarding near-spellings.
    """
    normalized = normalize_molecule_name(name)
    collapsed = normalized.replace(" ", "")
    return collapsed[:6]


def company_blocking_key(name: str) -> str:
    """Coarse key for company candidate generation."""
    normalized = normalize_company_name(name)
    collapsed = normalized.replace(" ", "")
    return collapsed[:5]
