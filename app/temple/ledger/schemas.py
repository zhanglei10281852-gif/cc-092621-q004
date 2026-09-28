from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, model_validator

Amount = Union[str, int, float]
# 金额在服务层用整数分校验（必须为正、最多到分），避免 pydantic 对字符串 Union 套用数值约束。


class DonationDesignation(BaseModel):
    scope: Literal["general", "temple", "hall", "campaign"] = "general"
    code: str | None = Field(default=None, max_length=80)
    hall_code: str | None = Field(default=None, max_length=64)
    campaign_code: str | None = Field(default=None, max_length=80)
    label: str | None = Field(default=None, max_length=160)
    amount: Amount


class DonationRegister(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    batch_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    amount: Amount
    credential: str = Field(min_length=4, max_length=160, description="银行回单/收据等业务凭据号，重放不重复入账")
    designations: list[DonationDesignation] = Field(default_factory=list, max_length=100)
    donor_key: str | None = Field(default=None, max_length=120)
    donor_name: str | None = Field(default=None, max_length=120)
    anonymous: bool = True
    external_reference: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=500)
    received_at: str | None = None
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_designations(self) -> "DonationRegister":
        for item in self.designations:
            if item.scope == "hall" and not (item.hall_code or item.code):
                raise ValueError("殿堂用途必须提供 hall_code")
            if item.scope == "campaign" and not (item.campaign_code or item.code):
                raise ValueError("修缮计划用途必须提供 campaign_code")
        return self


class DonationReverse(BaseModel):
    amount: Amount | None = Field(default=None)
    credential: str = Field(min_length=4, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class BudgetHoldCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    campaign_code: str = Field(min_length=3, max_length=80)
    amount: Amount
    credential: str = Field(min_length=4, max_length=160, description="预算承诺凭据号，重放不重复入账")
    memo: str | None = Field(default=None, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class ExpenditureCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    campaign_code: str = Field(min_length=3, max_length=80)
    amount: Amount
    credential: str = Field(min_length=4, max_length=160, description="付款凭据号，重放不重复入账")
    memo: str | None = Field(default=None, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class HoldRelease(BaseModel):
    amount: Amount | None = Field(default=None)
    credential: str = Field(min_length=4, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class TransferCreate(BaseModel):
    transfer_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    source_purpose_code: str = Field(min_length=3, max_length=80)
    target_purpose_code: str = Field(min_length=3, max_length=80)
    amount: Amount
    reason: str = Field(min_length=4, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class TransferDecision(BaseModel):
    credential: str = Field(min_length=4, max_length=160, description="审批确认凭据号，重放不重复审批")
    actor: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=500)
