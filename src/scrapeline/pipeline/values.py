"""Value types shared by records, normalisation and the schema registry. Pure, no I/O."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    """An amount in one currency. Serialised to JSON as {"amount": "51.77", "currency": "GBP"}."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
