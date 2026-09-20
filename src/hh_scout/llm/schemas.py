"""Pydantic models for AI answers."""

from __future__ import annotations

from pydantic import BaseModel, Field, RootModel, field_validator


class TriageVerdict(BaseModel):
    hh_id: str
    open: bool
    priority: int = Field(ge=1, le=3, default=3)
    reason: str = ""

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

