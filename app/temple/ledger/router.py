from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.temple.ledger.schemas import (
    BudgetHoldCreate,
    DonationRegister,
    DonationReverse,
    ExpenditureCreate,
    HoldRelease,
    TransferCreate,
    TransferDecision,
)
from app.temple.ledger.service import FundLedgerService

router = APIRouter(prefix="/api/temple/funds", tags=["专项资金台账"])


def service() -> FundLedgerService:
    return FundLedgerService()


def require_read(principal: Principal) -> None:
    principal.require("temple.read")


def require_write(principal: Principal) -> None:
    principal.require("temple.write")


# ---------------------------------------------------------------- 捐赠批次

@router.post("/donations", status_code=201)
def register_donation(payload: DonationRegister, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().register_donation(payload.model_dump())


@router.get("/donations")
def list_donations(temple_code: str | None = None, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return {"items": service().list_donations(temple_code)}


@router.get("/donations/{batch_code}")
def donation_detail(batch_code: str, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return service().donation_detail(batch_code)


@router.post("/donations/{batch_code}/reverse")
def reverse_donation(batch_code: str, payload: DonationReverse, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().reverse_donation(batch_code, payload.model_dump())


# ---------------------------------------------------------------- 预算承诺/支出/释放

@router.post("/budgets/holds", status_code=201)
def hold_budget(payload: BudgetHoldCreate, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().hold_budget(payload.model_dump())


@router.post("/budgets/holds/{hold_entry_id}/release")
def release_hold(hold_entry_id: int, payload: HoldRelease, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().release_hold(hold_entry_id, payload.model_dump())


@router.post("/budgets/expenditures", status_code=201)
def record_expenditure(payload: ExpenditureCreate, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().record_expenditure(payload.model_dump())


@router.get("/campaigns/{campaign_code}/budget")
def campaign_budget(
    campaign_code: str,
    temple_code: str | None = None,
    at: str | None = None,
    principal: Principal = Depends(current_principal),
):
    require_read(principal)
    return service().campaign_budget(campaign_code, temple_code, at=at)


# ---------------------------------------------------------------- 用途与流水

@router.get("/purposes")
def list_purposes(temple_code: str | None = None, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return {"items": service().list_purposes(temple_code)}


@router.get("/purposes/{code}")
def purpose_detail(code: str, at: str | None = None, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return service().purpose_detail(code, at=at)


@router.get("/ledger")
def ledger_entries(
    purpose_id: int | None = None,
    campaign_id: int | None = None,
    batch_id: int | None = None,
    at: str | None = None,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(current_principal),
):
    require_read(principal)
    return {"items": service().entries(
        purpose_id=purpose_id, campaign_id=campaign_id, batch_id=batch_id, at=at, after_id=after_id, limit=limit
    )}


@router.get("/verify")
def verify_integrity(principal: Principal = Depends(current_principal)):
    require_read(principal)
    return service().verify_integrity()


# ---------------------------------------------------------------- 跨用途调拨

@router.post("/transfers", status_code=201)
def create_transfer(payload: TransferCreate, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().create_transfer(payload.model_dump(), requester_user_id=principal.user_id)


@router.get("/transfers")
def list_transfers(state: str | None = None, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return {"items": service().list_transfers(state)}


@router.get("/transfers/{request_id}")
def transfer_detail(request_id: int, principal: Principal = Depends(current_principal)):
    require_read(principal)
    return service().transfer_detail(request_id)


@router.post("/transfers/{request_id}/approve")
def approve_transfer(request_id: int, payload: TransferDecision, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().approve_transfer(request_id, payload.model_dump(), approver_user_id=principal.user_id)


@router.post("/transfers/{request_id}/reject")
def reject_transfer(request_id: int, payload: TransferDecision, principal: Principal = Depends(current_principal)):
    require_write(principal)
    return service().reject_transfer(request_id, payload.model_dump(), approver_user_id=principal.user_id)
