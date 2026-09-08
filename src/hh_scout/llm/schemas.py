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
    employment_hint: str = Field(default="unknown", pattern="^(staff|project|unknown)$")
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
