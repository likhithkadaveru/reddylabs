"""Controlled vocabularies.

These are Python enums stored as strings. We deliberately do not use Postgres
native enums: adding a value to a native enum requires a migration and a lock,
and we expect to add event and signal types frequently.
"""

from __future__ import annotations

from enum import StrEnum


class AssertionLevel(StrEnum):
    """How strongly we are entitled to state something.

    The distinction the brief insisted on. FACT requires first-party evidence;
    INFERENCE must record its basis; UNKNOWN is displayed as such.
    """

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class VerificationLevel(StrEnum):
    """Whether a source contract has been observed or only documented."""

    LIVE_VERIFIED = "LIVE_VERIFIED"
    DOC_VERIFIED = "DOC_VERIFIED"
    ASSERTED = "ASSERTED"


class SourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANDIDATE = "CANDIDATE"
    RESTRICTED = "RESTRICTED"
    REJECTED = "REJECTED"


class AccessMode(StrEnum):
    API = "api"
    BULK = "bulk"
    MANUAL = "manual"


class EntityType(StrEnum):
    MOLECULE = "molecule"
    COMPANY = "company"
    DRUG_PRODUCT = "drug_product"
    REGULATORY_APPLICATION = "regulatory_application"
    CLINICAL_TRIAL = "clinical_trial"
    DRUG_MASTER_FILE = "drug_master_file"
    PATENT = "patent"
    EXCLUSIVITY = "exclusivity"
    INDICATION = "indication"
    COUNTRY = "country"


class IdentifierScheme(StrEnum):
    UNII = "UNII"
    RXCUI = "RXCUI"
    NDC = "NDC"
    FDA_APPL_NO = "FDA_APPL_NO"
    FDA_DMF_NO = "FDA_DMF_NO"
    NCT_ID = "NCT_ID"
    INN = "INN"
    CAS = "CAS"
    ISO3166A2 = "ISO3166A2"
    ORANGE_BOOK_PRODUCT = "ORANGE_BOOK_PRODUCT"


class AliasKind(StrEnum):
    LEGAL_NAME = "legal_name"
    TRADE_STYLE = "trade_style"
    ABBREVIATION = "abbreviation"
    FORMER_NAME = "former_name"
    SALT_FORM = "salt_form"
    SYNONYM = "synonym"
    SOURCE_LITERAL = "source_literal"


class ResolutionMethod(StrEnum):
    """How an entity link was decided. Every merge must name one of these."""

    DETERMINISTIC_IDENTIFIER = "deterministic_identifier"
    NORMALIZED_EXACT = "normalized_exact"
    ALIAS_TABLE = "alias_table"
    FUZZY = "fuzzy"
    CONTEXTUAL = "contextual"
    LLM_ASSISTED = "llm_assisted"
    HUMAN_REVIEW = "human_review"


class CompanyRole(StrEnum):
    NDA_HOLDER = "nda_holder"
    ANDA_APPLICANT = "anda_applicant"
    DMF_HOLDER = "dmf_holder"
    TRIAL_SPONSOR = "trial_sponsor"
    LABELLER = "labeller"
    SHORTAGE_REPORTER = "shortage_reporter"


class CapabilityType(StrEnum):
    API_SUPPLY = "api_supply"
    DOSAGE_FORM = "dosage_form"
    PEPTIDE_SYNTHESIS = "peptide_synthesis"
    HPAPI_HANDLING = "hpapi_handling"
    COMPLEX_CHEMISTRY = "complex_chemistry"
    BIOLOGICS = "biologics"
    MARKET_PRESENCE = "market_presence"
    THERAPEUTIC_AREA = "therapeutic_area"
    FINISHED_DOSE = "finished_dose"


class MoleculeClass(StrEnum):
    SMALL_MOLECULE = "small_molecule"
    PEPTIDE = "peptide"
    BIOLOGIC = "biologic"
    OTHER = "other"
    UNKNOWN = "unknown"


class Agency(StrEnum):
    FDA = "FDA"
    EMA = "EMA"
    CDSCO = "CDSCO"


class ApplicationType(StrEnum):
    NDA = "NDA"
    ANDA = "ANDA"
    BLA = "BLA"
    UNKNOWN = "UNKNOWN"


class EventType(StrEnum):
    """The temporal spine. Each value must be derivable from a dated source record."""

    NEW_TRIAL = "NEW_TRIAL"
    TRIAL_PHASE_CHANGED = "TRIAL_PHASE_CHANGED"
    TRIAL_STATUS_CHANGED = "TRIAL_STATUS_CHANGED"
    TRIAL_TERMINATED = "TRIAL_TERMINATED"
    NEW_APPROVAL = "NEW_APPROVAL"
    NEW_FILING = "NEW_FILING"
    NEW_PATENT_LISTING = "NEW_PATENT_LISTING"
    PATENT_DELISTED = "PATENT_DELISTED"
    NEW_EXCLUSIVITY = "NEW_EXCLUSIVITY"
    EXCLUSIVITY_EXPIRED = "EXCLUSIVITY_EXPIRED"
    NEW_COMPANY_ENTRY = "NEW_COMPANY_ENTRY"
    SHORTAGE_OPENED = "SHORTAGE_OPENED"
    SHORTAGE_RESOLVED = "SHORTAGE_RESOLVED"
    NEW_DMF = "NEW_DMF"
    DMF_STATUS_CHANGED = "DMF_STATUS_CHANGED"
    NEW_MARKET_ENTRY = "NEW_MARKET_ENTRY"
    MARKETING_ENDED = "MARKETING_ENDED"


class OpportunityType(StrEnum):
    SELL = "SELL"
    BUILD = "BUILD"


class PriorityBand(StrEnum):
    HIGH = "HIGH"
    WATCH = "WATCH"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvidenceConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


class SignalDirection(StrEnum):
    SUPPORTING = "SUPPORTING"
    COUNTER = "COUNTER"


class ExtractionMethod(StrEnum):
    DETERMINISTIC_PARSE = "deterministic_parse"
    LLM_EXTRACTION = "llm_extraction"
    HUMAN_CURATION = "human_curation"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
