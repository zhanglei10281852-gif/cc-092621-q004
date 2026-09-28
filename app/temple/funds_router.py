from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.funds import FundLedgerService
from app.temple.funds_schemas import (
    BudgetCommit,
    CommitmentRelease,
    DonationCreate,
    DonationReverse,
    ExpenditureCreate,
    FundCreate,
    TransferConfirm,
    TransferPropose,
    TransferReject,
)

router = APIRouter(prefix="/api/temple/funds", tags=["专项资金台账"])


def service() -> FundLedgerService:
    return FundLedgerService()


@router.post("", status_code=201)
def create_fund(payload: FundCreate):
    return service().create_fund(payload.model_dump())


@router.get("")
def list_funds(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_funds(temple_code, state)}


@router.get("/{fund_code}")
def fund_detail(fund_code: str, as_of: str | None = None):
    return service().fund_detail(fund_code, as_of=as_of)


@router.get("/{fund_code}/ledger")
def fund_ledger(
    fund_code: str,
    as_of: str | None = None,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return service().fund_ledger(fund_code, as_of=as_of, after_id=after_id, limit=limit)


@router.get("/{fund_code}/sources")
def fund_sources(fund_code: str, as_of: str | None = None):
    return service().fund_sources(fund_code, as_of=as_of)


@router.post("/donations", status_code=201)
def record_donation(payload: DonationCreate):
    return service().record_donation(payload.model_dump())


@router.post("/donations/reverse")
def reverse_donation(payload: DonationReverse):
    return service().reverse_donation(payload.model_dump())


@router.post("/commitments", status_code=201)
def commit_budget(payload: BudgetCommit):
    return service().commit_budget(payload.model_dump())


@router.post("/commitments/{commitment_code}/expenditures")
def record_expenditure(commitment_code: str, payload: ExpenditureCreate):
    return service().record_expenditure(commitment_code, payload.model_dump())


@router.post("/commitments/{commitment_code}/release")
def release_commitment(commitment_code: str, payload: CommitmentRelease):
    return service().release_commitment(commitment_code, payload.model_dump())


@router.get("/campaigns/{campaign_code}")
def campaign_finance(campaign_code: str):
    return service().campaign_finance(campaign_code)


@router.post("/transfers", status_code=201)
def propose_transfer(payload: TransferPropose):
    return service().propose_transfer(payload.model_dump())


@router.post("/transfers/{order_code}/confirm")
def confirm_transfer(order_code: str, payload: TransferConfirm):
    return service().confirm_transfer(order_code, payload.model_dump())


@router.post("/transfers/{order_code}/reject")
def reject_transfer(order_code: str, payload: TransferReject):
    return service().reject_transfer(order_code, payload.model_dump())


@router.get("/transfers/{order_code}")
def transfer_detail(order_code: str):
    return service().transfer_detail(order_code)


@router.get("/integrity/verify")
def verify_integrity(as_of: str | None = None):
    return service().verify_integrity(as_of=as_of)
