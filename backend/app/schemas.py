from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    decision_key: str = Field(min_length=8, max_length=128)
    comment: str = Field(default="", max_length=500)


class AdminLoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)

