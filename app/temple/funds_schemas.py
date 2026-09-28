from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FundCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    restriction_type: Literal["hall", "component", "unrestricted"]
    hall_code: str | None = Field(default=None, max_length=64)
    component_code: str | None = Field(default=None, max_length=80)
    actor: str = Field(min_length=1, max_length=120)


class DonationCreate(BaseModel):
    fund_code: str = Field(min_length=3, max_length=80)
    batch_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    receipt_no: str = Field(min_length=3, max_length=120)
    donor_ref: str = Field(min_length=2, max_length=200)
    amount: str = Field(min_length=1, max_length=24)
    received_at: str
    channel: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class DonationReverse(BaseModel):
    receipt_no: str = Field(min_length=3, max_length=120)
    amount: str = Field(min_length=1, max_length=24)
    command_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class BudgetCommit(BaseModel):
    campaign_code: str = Field(min_length=3, max_length=80)
    fund_code: str = Field(min_length=3, max_length=80)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    amount: str = Field(min_length=1, max_length=24)
    reason: str = Field(default="", max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class ExpenditureCreate(BaseModel):
    amount: str = Field(min_length=1, max_length=24)
    command_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=120)


class CommitmentRelease(BaseModel):
    amount: str | None = Field(default=None, min_length=1, max_length=24)
    command_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_amount(self) -> "CommitmentRelease":
        if self.amount is not None and not self.amount.strip():
            raise ValueError("释放金额不能为空字符串；不传表示释放全部占用")
        return self


class TransferPropose(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    command_key: str = Field(min_length=8, max_length=160)
    from_fund_code: str = Field(min_length=3, max_length=80)
    to_fund_code: str = Field(min_length=3, max_length=80)
    amount: str = Field(min_length=1, max_length=24)
    reason: str = Field(min_length=2, max_length=500)
    proposed_by: str = Field(min_length=1, max_length=120)
    required_approver: str = Field(min_length=1, max_length=120)


class TransferConfirm(BaseModel):
    approver: str = Field(min_length=1, max_length=120)
    command_key: str = Field(min_length=8, max_length=160)


class TransferReject(BaseModel):
    approver: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=500)
