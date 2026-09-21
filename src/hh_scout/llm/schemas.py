"""Pydantic models for AI answers."""

from __future__ import annotations

from pydantic import BaseModel, Field, RootModel, field_validator


class TriageVerdict(BaseModel):
    hh_id: str
    open: bool
    priority: int = Field(ge=1, le=3, default=3)
    reason: str = ""
    # v9.15 (decision #55): a closed card whose company runs automation itself (КИПиА, operations, electrical on a
    # production site) — the company becomes a `plant` lead even though the vacancy is not for a programmer.
    plant: bool = False

    @field_validator("hh_id", mode="before")
    @classmethod
    def _to_str(cls, v: object) -> str:
        return str(v)


class TriageBatch(RootModel[list[TriageVerdict]]):
    pass


class VacancyEvaluation(BaseModel):
    """A vacancy scored as a LEAD for the owner's contracting work (see prompts/vacancy_evaluation.md)."""

    hh_id: str
    tech_score: int = Field(ge=0, le=100)   # CODESYS/ST/MasterSCADA/PLC programming match
    role_score: int = Field(ge=0, le=100)   # they need a programmer, not a designer/maintenance
    lead_score: int = Field(ge=0, le=100)   # direct employer, contract-friendly signals
    ip_gph_possible: str = Field(pattern="^(yes|maybe|no)$")
    is_agency: bool = False
    company_kind: str = Field(default="unknown", pattern="^(integrator|manufacturer|end_customer|agency|unknown)$")
    verdict: str
    pitch_hint: str = ""
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("hh_id", mode="before")
    @classmethod
    def _to_str(cls, v: object) -> str:
        return str(v)


class EvaluationBatch(RootModel[list[VacancyEvaluation]]):
    pass


OFFER_FOCUS = ("plc_hmi_per_panel", "templates", "commissioning_scada", "plc_selection", "subcontract_programming")


class CompanyEvaluation(BaseModel):
    """A *company* scored as a lead for a partnership offer (v9.13, prompts/company_evaluation.md).

    No role score: the vacancy that revealed the company is not for a programmer. `fit_score` — is there work here
    that needs a PLC/HMI/SCADA program and can be handed to a contractor; `lead_score` — a direct company one can
    write to (not an agency), its scale and stack.
    """

    hh_id: str
    fit_score: int = Field(ge=0, le=100)
    lead_score: int = Field(ge=0, le=100)
    company_kind: str = Field(default="unknown",
                              pattern="^(panel_builder|design_bureau|integrator|manufacturer|end_customer|agency|unknown)$")
    verdict: str
    pitch_hint: str = ""
    offer_focus: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("hh_id", mode="before")
    @classmethod
    def _to_str(cls, v: object) -> str:
        return str(v)

    @field_validator("offer_focus")
    @classmethod
    def _known_focus(cls, v: list[str]) -> list[str]:
        return [x for x in v if x in OFFER_FOCUS]


class CompanyEvaluationBatch(RootModel[list[CompanyEvaluation]]):
    pass


class CompanyBrief(BaseModel):
    """What the open web says about an employer (see prompts/company_research.md).

    Facts and guesses are kept apart on purpose: every field but `automation_hooks` must come from a page the
    model actually read, and `sources` says which. `found=False` is a valid answer — a small company with no
    site is common, and inventing one would poison the letter.
    """

    found: bool = False
    what_they_do: str = ""
    industry: str = ""
    products: list[str] = Field(default_factory=list)
    sites: list[str] = Field(default_factory=list)
    scale: str = ""
    automation_hooks: list[str] = Field(default_factory=list)  # guesses, marked as such in the letter prompt
    sources: list[str] = Field(default_factory=list)
    note: str = ""
    # v9.15: the company's public contacts as read on its site or hh page — where a partnership offer can be sent
    # when the company was found through someone else's vacancy. General addresses only, never a person's.
    website: str = ""
    contact_email: str = ""
    contact_phone: str = ""

