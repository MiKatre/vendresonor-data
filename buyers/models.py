"""JSON contracts. Prices are decimal strings; unknown costs stay unknown."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Rate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rate_id: str
    purity_carats: Decimal | None = None
    fineness_per_mille: Decimal | None = None
    price_eur_per_gram: Decimal | None = None
    max_price_eur_per_gram: Decimal | None = None
    quote_kind: Literal["published", "indicative", "up_to", "range", "minimum_formula"]
    product_kind: str
    price_basis: str = "unknown"
    transaction_channel: str
    min_weight_g: Decimal | None = None
    max_weight_g: Decimal | None = None
    source_url: str
    observed_at: datetime
    source_updated_at: datetime | None = None
    source_date: str | None = None
    origin: Literal["displayed", "buyer_calculator", "formula"] = "displayed"
    source_fields: dict = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    comparison_eligible: bool = False

    @field_validator(
        "price_eur_per_gram", "max_price_eur_per_gram", "min_weight_g", "max_weight_g"
    )
    @classmethod
    def positive(cls, value):
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("must be positive and finite")
        return value

    @field_validator("purity_carats", "fineness_per_mille")
    @classmethod
    def purity(cls, value, info):
        maximum = 24 if info.field_name == "purity_carats" else 1000
        if value is not None and (not value.is_finite() or not 0 < value <= maximum):
            raise ValueError("invalid purity")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.purity_carats is None and self.fineness_per_mille is None:
            raise ValueError("purity required")
        if self.quote_kind == "minimum_formula":
            if self.price_eur_per_gram is not None or not self.source_fields:
                raise ValueError(
                    "formula must retain source fields without an invented price"
                )
        elif self.price_eur_per_gram is None:
            raise ValueError("numeric quote requires a price")
        if self.quote_kind == "range" and (
            self.max_price_eur_per_gram is None
            or self.max_price_eur_per_gram < self.price_eur_per_gram
        ):
            raise ValueError("invalid price range")
        if (
            self.max_weight_g is not None
            and self.min_weight_g is not None
            and self.max_weight_g < self.min_weight_g
        ):
            raise ValueError("invalid weight range")
        if self.observed_at.utcoffset() is None:
            raise ValueError("observation timestamp must include timezone")
        if self.source_updated_at is not None and (
            self.source_updated_at.utcoffset() is None
            or self.source_updated_at > self.observed_at
        ):
            raise ValueError("invalid or future source timestamp")
        return self


class Observation(BaseModel):
    buyer_id: str
    observed_at: datetime
    rates: list[Rate] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    documents: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        keys = [r.rate_id for r in self.rates]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate rate ids")
        return self
