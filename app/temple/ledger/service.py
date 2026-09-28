from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import OrderedDict
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.privacy import sanitize_text
from app.database import get_connection, transaction
from app.temple.ledger.money import to_cents, yuan
from app.temple.schema import ensure_temple_schema

DONOR_SALT = os.getenv("TEMPLE_DONOR_SALT", "temple-donor-ledger")

ENTRY_EFFECTS: dict[str, tuple[int, int]] = {
    # event_type: (账面余额方向, 已承诺方向)
    "donation_in": (1, 0),
    "donation_reversal": (-1, 0),
    "reservation_hold": (0, 1),
    "reservation_release": (0, -1),
    "expenditure": (-1, -1),
    "transfer_out": (-1, 0),
    "transfer_in": (1, 0),
}

SCOPE_LABELS = {"general": "寺院通用", "temple": "寺院专项", "hall": "殿堂修缮", "campaign": "修缮计划"}


def donor_hash(donor_key: str) -> str:
    return hashlib.sha256((DONOR_SALT + "|" + donor_key.strip()).encode()).hexdigest()


def credential_fingerprint(credential: str) -> str:
    return hashlib.sha256(("ledger-credential|" + credential.strip()).encode()).hexdigest()


def proportional_split(amount: int, weights: list[int]) -> list[int]:
    """按权重把整数分拆分到各项，最大余数法补差，合计恒等于 amount。"""
    total = sum(weights)
    if total <= 0:
        raise ConflictError("没有可分摊的资金")
    shares = [amount * weight // total for weight in weights]
    remainder = amount - sum(shares)
    ranked = sorted(
        range(len(weights)),
        key=lambda index: (amount * weights[index] % total, weights[index]),
        reverse=True,
    )
    for index in ranked[:remainder]:
        shares[index] += 1
    return shares


class FundLedgerService:
    """专项资金台账。

    账面余额与已承诺金额永远只从只追加的 ledger_entries 复算；每条消耗类流水
    携带按资金来源（捐赠批次/调入凭据）的分摊明细，因此任意时点都能复算出
    相同的账面余额、已承诺、可支配与资金来源构成。所有写操作在一个即时事务
    内提交并校验不变量，失败整体回滚。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()

    # ================================================================ 用途

    def purpose_by_code(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM fund_purposes WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("资金用途不存在")
        return row

    def campaign_purpose_id(self, connection: sqlite3.Connection, campaign_id: int, *, create: bool = False) -> int | None:
        row = connection.execute(
            "SELECT id FROM fund_purposes WHERE scope_type='campaign' AND scope_id=?", (campaign_id,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        if not create:
            return None
        campaign = connection.execute("SELECT id,name,temple_id FROM restoration_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise NotFoundError("修缮计划不存在")
        return self._ensure_purpose(connection, "campaign", int(campaign["id"]), campaign["name"], int(campaign["temple_id"]))

    def list_purposes(self, temple_code: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT p.*,n.code AS temple_code,n.name AS temple_name FROM fund_purposes p "
            "LEFT JOIN temple_sites n ON n.id=p.temple_id"
        )
        params: list[Any] = []
        if temple_code:
            temple = self.connection.execute("SELECT id FROM temple_sites WHERE code=?", (temple_code,)).fetchone()
            if temple is None:
                raise NotFoundError("寺院不存在")
            sql += " WHERE p.temple_id=?"
            params.append(temple["id"])
        sql += " ORDER BY p.id"
        result = []
        for row in self.connection.execute(sql, params).fetchall():
            item = dict(row)
            item.update(self._balances(self.connection, int(row["id"])))
            result.append(item)
        return result

    def purpose_detail(self, code: str, at: str | None = None) -> dict[str, Any]:
        purpose = self.purpose_by_code(code)
        result = dict(purpose)
        result.update(self.balances(int(purpose["id"]), at=at))
        result["sources"] = self.source_tracking(int(purpose["id"]), at=at)
        return result

    @staticmethod
    def _purpose_code(scope_type: str, scope_id: int) -> str:
        return f"{scope_type}-{scope_id}"

    def _ensure_purpose(
        self, connection: sqlite3.Connection, scope_type: str, scope_id: int, label: str = "", temple_id: int | None = None
    ) -> int:
        code = self._purpose_code(scope_type, scope_id)
        row = connection.execute("SELECT id FROM fund_purposes WHERE code=?", (code,)).fetchone()
        if row is not None:
            return int(row["id"])
        now = to_storage(self.clock.now())
        if temple_id is None:
            temple_id = scope_id if scope_type in {"general", "temple"} else None
        cursor = connection.execute(
            "INSERT INTO fund_purposes(scope_type,temple_id,scope_id,code,label,created_at) VALUES(?,?,?,?,?,?)",
            (scope_type, temple_id, scope_id, code, label or SCOPE_LABELS.get(scope_type, ""), now),
        )
        return int(cursor.lastrowid)

    def _resolve_purpose(self, connection: sqlite3.Connection, temple_id: int, target: dict[str, Any]) -> int:
        scope = target.get("scope", "general")
        if scope == "general":
            return self._ensure_purpose(connection, "general", temple_id, SCOPE_LABELS["general"], temple_id)
        if scope == "temple":
            return self._ensure_purpose(connection, "temple", temple_id, target.get("label") or SCOPE_LABELS["temple"], temple_id)
        if scope == "hall":
            code = target.get("hall_code") or target.get("code")
            hall = connection.execute("SELECT id,name FROM worship_halls WHERE temple_id=? AND code=?", (temple_id, code)).fetchone()
            if hall is None:
                raise NotFoundError(f"用途殿堂不存在：{code}")
            return self._ensure_purpose(connection, "hall", int(hall["id"]), f"{hall['name']}修缮", temple_id)
        if scope == "campaign":
            code = target.get("campaign_code") or target.get("code")
            campaign = connection.execute("SELECT id,name FROM restoration_campaigns WHERE temple_id=? AND code=?", (temple_id, code)).fetchone()
            if campaign is None:
                raise NotFoundError(f"用途修缮计划不存在：{code}")
            return self._ensure_purpose(connection, "campaign", int(campaign["id"]), campaign["name"], temple_id)
        raise ValidationError("未知资金用途范围")

    # ================================================================ 捐赠批次

    def register_donation(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (payload["temple_code"],)).fetchone()
        if temple is None:
            raise NotFoundError("寺院不存在")
        gross_cents = self._amount(payload["amount"], "捐赠金额")
        designation_inputs = payload.get("designations") or []
        for item in designation_inputs:
            item["_cents"] = self._amount(item["amount"], "用途金额")
        total = sum(item["_cents"] for item in designation_inputs) if designation_inputs else gross_cents
        if total != gross_cents:
            raise ValidationError(f"用途分配合计 {yuan(total)} 与捐赠金额 {yuan(gross_cents)} 不一致")
        received_at = self._optional_time(payload.get("received_at"), "到账时间") or to_storage(self.clock.now())
        credential_hash = self._require_credential(payload.get("credential"))
        anonymous = bool(payload.get("anonymous", True))
        donor_key = (payload.get("donor_key") or payload.get("donor_name") or "anonymous-donor").strip()
        donor_label = "匿名捐赠方" if anonymous else sanitize_text((payload.get("donor_name") or "善心人士").strip())
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._register_credential(connection, credential_hash, "donation", 0, now)
            try:
                cursor = connection.execute(
                    "INSERT INTO donation_batches(temple_id,code,donor_key_hash,donor_label,anonymous,gross_amount_cents,"
                    "external_reference,note,received_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(temple["id"]), payload["batch_code"], donor_hash(donor_key), donor_label, int(anonymous),
                     gross_cents, (payload.get("external_reference") or "").strip(),
                     sanitize_text(payload.get("note") or ""), received_at, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("捐赠批次号已存在") from exc
            batch_id = int(cursor.lastrowid)
            targets = designation_inputs or [{"scope": "general", "_cents": gross_cents}]
            touched: set[int] = set()
            for ordinal, item in enumerate(targets):
                purpose_id = self._resolve_purpose(connection, int(temple["id"]), item)
                connection.execute(
                    "INSERT INTO donation_designations(batch_id,purpose_id,amount_cents,ordinal) VALUES(?,?,?,?)",
                    (batch_id, purpose_id, item["_cents"], ordinal),
                )
                self._insert_entry(
                    connection,
                    purpose_id=purpose_id,
                    batch_id=batch_id,
                    event_type="donation_in",
                    amount=item["_cents"],
                    actor=payload["actor"],
                    memo=f"捐赠批次 {payload['batch_code']} 入账",
                    created_at=received_at,
                )
                touched.add(purpose_id)
            self._assert_invariants(connection, touched)
        return self.donation_detail(payload["batch_code"])

    def reverse_donation(self, batch_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        batch = self.connection.execute("SELECT * FROM donation_batches WHERE code=?", (batch_code,)).fetchone()
        if batch is None:
            raise NotFoundError("捐赠批次不存在")
        if batch["state"] != "posted":
            raise ConflictError("捐赠批次已全部撤销，不能再次撤销")
        remaining = int(batch["gross_amount_cents"]) - int(batch["reversed_amount_cents"])
        amount = self._amount(payload["amount"], "撤销金额") if payload.get("amount") else remaining
        if amount > remaining:
            raise ValidationError(f"撤销金额不能超过未撤销余额 {yuan(remaining)}")
        credential_hash = self._require_credential(payload.get("credential"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._register_credential(connection, credential_hash, "donation_reversal", int(batch["id"]), now)
            designations = connection.execute(
                "SELECT * FROM donation_designations WHERE batch_id=? ORDER BY id", (batch["id"],)
            ).fetchall()
            # 只能撤销仍可动用的资金；权重为各用途下该批次当前可动用余额。
            weights: list[int] = []
            for designation in designations:
                lots = self._replay_lots(connection, int(designation["purpose_id"]))
                weights.append(sum(
                    lot["available"] for lot in lots.values() if lot["batch_id"] == batch["id"]
                ))
            if sum(weights) < amount:
                raise ConflictError("该捐赠中已有资金被预算占用、支出或调拨，不能撤销")
            slices = proportional_split(amount, weights)
            touched: set[int] = set()
            for designation, slice_amount in zip(designations, slices, strict=True):
                if slice_amount <= 0:
                    continue
                purpose_id = int(designation["purpose_id"])
                allocation = self._consume_lots(
                    connection, purpose_id, slice_amount,
                    lot_filter=lambda lot: lot["batch_id"] == batch["id"],
                )
                original = connection.execute(
                    "SELECT id FROM ledger_entries WHERE purpose_id=? AND batch_id=? AND event_type='donation_in' ORDER BY id LIMIT 1",
                    (purpose_id, batch["id"]),
                ).fetchone()
                self._insert_entry(
                    connection,
                    purpose_id=purpose_id,
                    batch_id=int(batch["id"]),
                    event_type="donation_reversal",
                    amount=slice_amount,
                    actor=payload["actor"],
                    memo=payload.get("reason") or "捐赠撤销/退款",
                    related_entry_id=int(original["id"]) if original else None,
                    allocation=allocation,
                    created_at=now,
                )
                connection.execute(
                    "UPDATE donation_designations SET reversed_amount_cents=reversed_amount_cents+? WHERE id=?",
                    (slice_amount, designation["id"]),
                )
                touched.add(purpose_id)
            reversed_total = int(batch["reversed_amount_cents"]) + amount
            state = "reversed" if reversed_total >= int(batch["gross_amount_cents"]) else "posted"
            connection.execute(
                "UPDATE donation_batches SET reversed_amount_cents=?,state=?,updated_at=? WHERE id=?",
                (reversed_total, state, now, batch["id"]),
            )
            self._assert_invariants(connection, touched)
        return self.donation_detail(batch_code)

    def donation_detail(self, batch_code: str) -> dict[str, Any]:
        batch = self.connection.execute(
            "SELECT b.*,n.code AS temple_code,n.name AS temple_name FROM donation_batches b "
            "JOIN temple_sites n ON n.id=b.temple_id WHERE b.code=?",
            (batch_code,),
        ).fetchone()
        if batch is None:
            raise NotFoundError("捐赠批次不存在")
        result = dict(batch)
        result["gross_amount"] = yuan(batch["gross_amount_cents"])
        result["reversed_amount"] = yuan(batch["reversed_amount_cents"])
        result.pop("donor_key_hash", None)
        if result.get("anonymous"):
            result["donor_label"] = "匿名捐赠方"
        result["designations"] = [
            {
                **dict(row),
                "amount": yuan(row["amount_cents"]),
                "reversed_amount": yuan(row["reversed_amount_cents"]),
            }
            for row in self.connection.execute(
                "SELECT d.*,p.code AS purpose_code,p.scope_type,p.label FROM donation_designations d "
                "JOIN fund_purposes p ON p.id=d.purpose_id WHERE d.batch_id=? ORDER BY d.id",
                (batch["id"],),
            ).fetchall()
        ]
        result["entries"] = self.entries(batch_id=int(batch["id"]))
        return result

    def list_donations(self, temple_code: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT b.*,n.code AS temple_code FROM donation_batches b JOIN temple_sites n ON n.id=b.temple_id"
        params: list[Any] = []
        if temple_code:
            sql += " WHERE n.code=?"
            params.append(temple_code)
        sql += " ORDER BY b.id DESC"
        items = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        for item in items:
            item["gross_amount"] = yuan(item.pop("gross_amount_cents"))
            item["reversed_amount"] = yuan(item.pop("reversed_amount_cents"))
            item.pop("donor_key_hash", None)
            if item.get("anonymous"):
                item["donor_label"] = "匿名捐赠方"
        return items

    # ================================================================ 预算承诺/支出/释放

    def hold_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self._campaign(payload["campaign_code"], payload.get("temple_code"))
        amount = self._amount(payload["amount"], "承诺金额")
        credential_hash = self._require_credential(payload.get("credential"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            purpose_id = self.campaign_purpose_id(connection, int(campaign["id"]), create=True)
            self._register_credential(connection, credential_hash, "budget_hold", int(campaign["id"]), now)
            allocation = self._consume_lots(connection, purpose_id, amount)
            entry_id = self._insert_entry(
                connection,
                purpose_id=purpose_id,
                event_type="reservation_hold",
                amount=amount,
                campaign_id=int(campaign["id"]),
                actor=payload["actor"],
                memo=payload.get("memo") or f"修缮计划 {campaign['code']} 预算承诺",
                allocation=allocation,
                created_at=now,
            )
            self._assert_invariants(connection, {purpose_id})
        return self.entry_detail(entry_id)

    def record_expenditure(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self._campaign(payload["campaign_code"], payload.get("temple_code"))
        amount = self._amount(payload["amount"], "支出金额")
        credential_hash = self._require_credential(payload.get("credential"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            purpose_id = self.campaign_purpose_id(connection, int(campaign["id"]))
            if purpose_id is None:
                raise ConflictError("修缮计划还没有预算承诺，不能登记支出")
            held = self._held_open(connection, int(campaign["id"]))
            if held < amount:
                raise ConflictError(
                    "实际支出不能超过已承诺未释放预算",
                    context={"committed_open": yuan(held), "requested": yuan(amount)},
                )
            self._register_credential(connection, credential_hash, "expenditure", int(campaign["id"]), now)
            allocation, first_hold = self._consume_hold_cells(connection, int(campaign["id"]), amount)
            entry_id = self._insert_entry(
                connection,
                purpose_id=purpose_id,
                event_type="expenditure",
                amount=amount,
                campaign_id=int(campaign["id"]),
                actor=payload["actor"],
                memo=payload.get("memo") or f"修缮计划 {campaign['code']} 实际支出",
                allocation=allocation,
                related_entry_id=first_hold,
                created_at=now,
            )
            self._assert_invariants(connection, {purpose_id})
        return self.entry_detail(entry_id)

    def release_hold(self, hold_entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        hold = self.connection.execute(
            "SELECT * FROM ledger_entries WHERE id=? AND event_type='reservation_hold'", (hold_entry_id,)
        ).fetchone()
        if hold is None:
            raise NotFoundError("预算承诺记录不存在")
        requested_amount = self._amount(payload["amount"], "释放金额") if payload.get("amount") else None
        credential_hash = self._require_credential(payload.get("credential"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            # 事务内重新读取占用单元，避免并发支出/释放造成失配
            open_cells = self._hold_open_cells(connection, hold_entry_id)
            open_amount = sum(cell["remaining"] for cell in open_cells)
            if open_amount <= 0:
                raise ConflictError("该笔预算承诺已全部支出或释放")
            amount = requested_amount or open_amount
            if amount > open_amount:
                raise ValidationError(f"释放金额不能超过未使用承诺 {yuan(open_amount)}")
            self._register_credential(connection, credential_hash, "budget_release", hold_entry_id, now)
            allocation, _ = self._take_cells(
                [
                    {"hold_entry_id": hold_entry_id, "lot_entry_id": cell["lot_entry_id"],
                     "batch_id": cell["batch_id"], "remaining": cell["remaining"]}
                    for cell in open_cells
                ],
                amount,
            )
            entry_id = self._insert_entry(
                connection,
                purpose_id=int(hold["purpose_id"]),
                event_type="reservation_release",
                amount=amount,
                campaign_id=hold["campaign_id"],
                actor=payload["actor"],
                memo=payload.get("reason") or "释放预算占用",
                allocation=allocation,
                related_entry_id=hold_entry_id,
                created_at=now,
            )
            self._assert_invariants(connection, {int(hold["purpose_id"])})
        return self.entry_detail(entry_id)

    def release_campaign_budget(
        self, campaign_id: int, actor: str, reason: str, connection: sqlite3.Connection | None = None
    ) -> list[int]:
        """项目完成或取消时释放全部尚未使用的预算承诺；可并入外部事务。"""
        if connection is None:
            with transaction(immediate=True) as conn:
                return self._release_campaign_budget(conn, campaign_id, actor, reason)
        return self._release_campaign_budget(connection, campaign_id, actor, reason)

    def _release_campaign_budget(self, connection: sqlite3.Connection, campaign_id: int, actor: str, reason: str) -> list[int]:
        purpose_id = self.campaign_purpose_id(connection, campaign_id)
        if purpose_id is None:
            return []
        now = to_storage(self.clock.now())
        release_ids: list[int] = []
        holds = connection.execute(
            "SELECT id FROM ledger_entries WHERE campaign_id=? AND event_type='reservation_hold' ORDER BY id",
            (campaign_id,),
        ).fetchall()
        for hold in holds:
            cells = self._hold_open_cells(connection, int(hold["id"]))
            open_amount = sum(cell["remaining"] for cell in cells)
            if open_amount <= 0:
                continue
            allocation, _ = self._take_cells(
                [
                    {"hold_entry_id": int(hold["id"]), "lot_entry_id": cell["lot_entry_id"],
                     "batch_id": cell["batch_id"], "remaining": cell["remaining"]}
                    for cell in cells
                ],
                open_amount,
            )
            entry_id = self._insert_entry(
                connection,
                purpose_id=purpose_id,
                event_type="reservation_release",
                amount=open_amount,
                campaign_id=campaign_id,
                actor=actor,
                memo=reason,
                allocation=allocation,
                related_entry_id=int(hold["id"]),
                created_at=now,
            )
            release_ids.append(entry_id)
        if release_ids:
            self._assert_invariants(connection, {purpose_id})
        return release_ids

    def campaign_budget(self, campaign_code: str, temple_code: str | None = None, at: str | None = None) -> dict[str, Any]:
        campaign = self._campaign(campaign_code, temple_code)
        purpose = self.connection.execute(
            "SELECT * FROM fund_purposes WHERE scope_type='campaign' AND scope_id=?", (campaign["id"],)
        ).fetchone()
        result: dict[str, Any] = {
            "campaign_id": int(campaign["id"]),
            "campaign_code": campaign["code"],
            "campaign_name": campaign["name"],
            "state": campaign["state"],
            "purpose": dict(purpose) if purpose else None,
        }
        zero = {"book_balance_cents": 0, "book_balance": "0.00", "committed_cents": 0, "committed": "0.00",
                "available_cents": 0, "available": "0.00", "as_of": self._point_in_time(at)}
        if purpose is None:
            result.update(zero, spent="0.00", spent_cents=0, ledger=[], sources=[])
            return result
        result.update(self.balances(int(purpose["id"]), at=at))
        spent = self._sum_event("expenditure", int(purpose["id"]), int(campaign["id"]), at=at)
        result["spent_cents"] = spent
        result["spent"] = yuan(spent)
        result["ledger"] = self.entries(purpose_id=int(purpose["id"]), at=at)
        result["sources"] = self.source_tracking(int(purpose["id"]), at=at)
        return result

    # ================================================================ 跨用途调拨（双重确认）

    def create_transfer(self, payload: dict[str, Any], *, requester_user_id: int) -> dict[str, Any]:
        amount = self._amount(payload["amount"], "调拨金额")
        source = self.purpose_by_code(payload["source_purpose_code"])
        target = self.purpose_by_code(payload["target_purpose_code"])
        if source["id"] == target["id"]:
            raise ValidationError("调出与调入用途不能相同")
        if len((payload.get("reason") or "").strip()) < 4:
            raise ValidationError("请说明跨用途调拨原因（至少 4 个字符）")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            balances = self._balances(connection, int(source["id"]))
            if amount > balances["available_cents"]:
                raise ConflictError(
                    "调出用途可支配资金不足",
                    context={"available": yuan(balances["available_cents"]), "requested": yuan(amount)},
                )
            try:
                cursor = connection.execute(
                    "INSERT INTO fund_transfer_requests(code,source_purpose_id,target_purpose_id,amount_cents,reason,"
                    "requested_by,requested_by_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (payload["transfer_code"], int(source["id"]), int(target["id"]), amount,
                     payload["reason"].strip(), payload["actor"], requester_user_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("调拨申请编号已存在") from exc
            request_id = int(cursor.lastrowid)
        return self.transfer_detail(request_id)

    def approve_transfer(self, request_id: int, payload: dict[str, Any], *, approver_user_id: int) -> dict[str, Any]:
        request = self.connection.execute("SELECT * FROM fund_transfer_requests WHERE id=?", (request_id,)).fetchone()
        if request is None:
            raise NotFoundError("调拨申请不存在")
        if request["state"] != "pending":
            raise ConflictError("调拨申请已处理，不能重复审批")
        if approver_user_id == int(request["requested_by_user_id"]):
            raise ConflictError("跨用途调拨必须由申请人之外的另一名有权人员双重确认")
        credential_hash = self._require_credential(payload.get("credential"))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._register_credential(connection, credential_hash, "transfer_approval", request_id, now)
            balances = self._balances(connection, int(request["source_purpose_id"]))
            if int(request["amount_cents"]) > balances["available_cents"]:
                raise ConflictError(
                    "调出用途可支配资金已不足，调拨失败",
                    context={"available": yuan(balances["available_cents"]), "requested": yuan(request["amount_cents"])},
                )
            source_allocation = self._consume_lots(connection, int(request["source_purpose_id"]), int(request["amount_cents"]))
            out_id = self._insert_entry(
                connection,
                purpose_id=int(request["source_purpose_id"]),
                event_type="transfer_out",
                amount=int(request["amount_cents"]),
                actor=payload["actor"],
                memo=f"调拨 {request['code']} 调出：{request['reason']}",
                allocation=source_allocation,
                transfer_request_id=request_id,
                created_at=now,
            )
            provenance = [
                {"lot_entry_id": item["lot_entry_id"], "batch_id": item["batch_id"], "amount_cents": item["amount_cents"]}
                for item in source_allocation
            ]
            in_id = self._insert_entry(
                connection,
                purpose_id=int(request["target_purpose_id"]),
                event_type="transfer_in",
                amount=int(request["amount_cents"]),
                actor=payload["actor"],
                memo=f"调拨 {request['code']} 调入：{request['reason']}",
                allocation=provenance,
                related_entry_id=out_id,
                transfer_request_id=request_id,
                created_at=now,
            )
            connection.execute("UPDATE ledger_entries SET related_entry_id=? WHERE id=?", (in_id, out_id))
            connection.execute(
                "UPDATE fund_transfer_requests SET state='approved',approved_by=?,approved_at=?,"
                "out_entry_id=?,in_entry_id=?,updated_at=? WHERE id=?",
                (payload["actor"], now, out_id, in_id, now, request_id),
            )
            self._assert_invariants(connection, {int(request["source_purpose_id"]), int(request["target_purpose_id"])})
        return self.transfer_detail(request_id)

    def reject_transfer(self, request_id: int, payload: dict[str, Any], *, approver_user_id: int) -> dict[str, Any]:
        request = self.connection.execute("SELECT * FROM fund_transfer_requests WHERE id=?", (request_id,)).fetchone()
        if request is None:
            raise NotFoundError("调拨申请不存在")
        if request["state"] != "pending":
            raise ConflictError("调拨申请已处理")
        if approver_user_id == int(request["requested_by_user_id"]):
            raise ConflictError("申请人不能自行审批调拨")
        if len((payload.get("reason") or "").strip()) < 2:
            raise ValidationError("请填写驳回原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE fund_transfer_requests SET state='rejected',approved_by=?,reject_reason=?,approved_at=?,updated_at=? WHERE id=?",
                (payload["actor"], payload["reason"].strip(), now, now, request_id),
            )
        return self.transfer_detail(request_id)

    def transfer_detail(self, request_id: int) -> dict[str, Any]:
        request = self.connection.execute(
            "SELECT r.*,sp.code AS source_purpose_code,tp.code AS target_purpose_code FROM fund_transfer_requests r "
            "JOIN fund_purposes sp ON sp.id=r.source_purpose_id JOIN fund_purposes tp ON tp.id=r.target_purpose_id WHERE r.id=?",
            (request_id,),
        ).fetchone()
        if request is None:
            raise NotFoundError("调拨申请不存在")
        result = dict(request)
        result["amount"] = yuan(request["amount_cents"])
        result.pop("credential_hash", None)
        return result

    def list_transfers(self, state: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*,sp.code AS source_purpose_code,tp.code AS target_purpose_code FROM fund_transfer_requests r "
            "JOIN fund_purposes sp ON sp.id=r.source_purpose_id JOIN fund_purposes tp ON tp.id=r.target_purpose_id"
        )
        params: list[Any] = []
        if state:
            sql += " WHERE r.state=?"
            params.append(state)
        sql += " ORDER BY r.id DESC"
        items = [dict(row) for row in self.connection.execute(sql, params).fetchall()]
        for item in items:
            item["amount"] = yuan(item.pop("amount_cents"))
            item.pop("credential_hash", None)
        return items

    # ================================================================ 查询

    def balances(self, purpose_id: int, at: str | None = None) -> dict[str, Any]:
        at_storage = self._point_in_time(at)
        balance, committed = self._totals(self.connection, purpose_id, at_storage)
        return {
            "book_balance_cents": balance,
            "book_balance": yuan(balance),
            "committed_cents": committed,
            "committed": yuan(committed),
            "available_cents": balance - committed,
            "available": yuan(balance - committed),
            "as_of": at_storage,
        }

    def _balances(self, connection: sqlite3.Connection, purpose_id: int) -> dict[str, int]:
        balance, committed = self._totals(connection, purpose_id, None)
        return {
            "book_balance_cents": balance,
            "committed_cents": committed,
            "available_cents": balance - committed,
        }

    def entries(
        self,
        *,
        purpose_id: int | None = None,
        campaign_id: int | None = None,
        batch_id: int | None = None,
        at: str | None = None,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["e.id>?"]
        params: list[Any] = [after_id]
        if purpose_id is not None:
            clauses.append("e.purpose_id=?")
            params.append(purpose_id)
        if campaign_id is not None:
            clauses.append("e.campaign_id=?")
            params.append(campaign_id)
        if batch_id is not None:
            clauses.append("e.batch_id=?")
            params.append(batch_id)
        at_storage = self._point_in_time(at)
        if at_storage:
            clauses.append("e.created_at<=?")
            params.append(at_storage)
        rows = self.connection.execute(
            "SELECT e.*,p.code AS purpose_code,p.scope_type FROM ledger_entries e "
            "JOIN fund_purposes p ON p.id=e.purpose_id WHERE " + " AND ".join(clauses) + " ORDER BY e.id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._entry_dict(row) for row in rows]

    def entry_detail(self, entry_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT e.*,p.code AS purpose_code,p.scope_type FROM ledger_entries e "
            "JOIN fund_purposes p ON p.id=e.purpose_id WHERE e.id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("台账流水不存在")
        result = self._entry_dict(row)
        result["purpose_balances"] = self.balances(int(row["purpose_id"]))
        return result

    def source_tracking(self, purpose_id: int, at: str | None = None) -> list[dict[str, Any]]:
        """按资金来源（捐赠批次/调拨凭据）复算可用、占用与已消耗构成。"""
        lots = self._replay_lots(self.connection, purpose_id, at=self._point_in_time(at))
        result: list[dict[str, Any]] = []
        for lot in lots.values():
            consumed = lot["gross"] - lot["available"] - lot["committed"]
            common = {
                "lot_entry_id": lot["lot_entry_id"],
                "gross_cents": lot["gross"],
                "gross": yuan(lot["gross"]),
                "available_cents": lot["available"],
                "available": yuan(lot["available"]),
                "committed_cents": lot["committed"],
                "committed": yuan(lot["committed"]),
                "consumed_cents": consumed,
                "consumed": yuan(consumed),
            }
            if lot["batch_id"] is None:
                common["source_type"] = "transfer"
                provenance = lot.get("provenance") or []
                common["origin_sources"] = [self._donation_source_ref(item["batch_id"]) for item in provenance]
                result.append(common)
                continue
            batch = self.connection.execute(
                "SELECT code,donor_label,anonymous,received_at FROM donation_batches WHERE id=?",
                (lot["batch_id"],),
            ).fetchone()
            if batch is None:  # pragma: no cover
                continue
            common.update({
                "source_type": "donation",
                "batch_id": int(lot["batch_id"]),
                "batch_code": batch["code"],
                "donor_label": "匿名捐赠方" if batch["anonymous"] else batch["donor_label"],
                "received_at": batch["received_at"],
            })
            result.append(common)
        return result

    def _donation_source_ref(self, batch_id: int | None) -> dict[str, Any]:
        if batch_id is None:
            return {"batch_id": None}
        batch = self.connection.execute(
            "SELECT code,donor_label,anonymous,received_at FROM donation_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return {"batch_id": batch_id}
        return {
            "batch_id": int(batch_id),
            "batch_code": batch["code"],
            "donor_label": "匿名捐赠方" if batch["anonymous"] else batch["donor_label"],
            "received_at": batch["received_at"],
        }

    def verify_integrity(self) -> dict[str, Any]:
        """全量复算校验：流水汇总与逐来源复算一致，且无负值或超占。"""
        violations: list[dict[str, Any]] = []
        purposes = self.connection.execute("SELECT id FROM fund_purposes").fetchall()
        for purpose in purposes:
            purpose_id = int(purpose["id"])
            balance, committed = self._totals(self.connection, purpose_id, None)
            if balance < 0 or committed < 0 or committed > balance:
                violations.append({"purpose_id": purpose_id, "book_balance": balance, "committed": committed})
            try:
                lots = self._replay_lots(self.connection, purpose_id)
            except ConflictError as exc:
                violations.append({"purpose_id": purpose_id, "reason": exc.message})
                continue
            lot_balance = sum(lot["available"] + lot["committed"] for lot in lots.values())
            lot_committed = sum(lot["committed"] for lot in lots.values())
            if lot_balance != balance or lot_committed != committed:
                violations.append({
                    "purpose_id": purpose_id,
                    "reason": "流水汇总与资金来源复算不一致",
                    "book_balance": balance,
                    "lot_balance": lot_balance,
                    "committed": committed,
                    "lot_committed": lot_committed,
                })
        return {"ok": not violations, "violations": violations, "purposes_checked": len(purposes)}

    # ================================================================ 内部：来源批次复算

    def _replay_lots(
        self, connection: sqlite3.Connection, purpose_id: int, at: str | None = None
    ) -> "OrderedDict[int, dict[str, Any]]":
        """逐笔重放用途流水，得到每个资金来源批次的 available/committed。

        lot 以入账流水（donation_in/transfer_in）的 entry id 为键。
        """
        sql = "SELECT * FROM ledger_entries WHERE purpose_id=?"
        params: list[Any] = [purpose_id]
        if at:
            sql += " AND created_at<=?"
            params.append(at)
        rows = connection.execute(sql + " ORDER BY id", params).fetchall()
        lots: "OrderedDict[int, dict[str, Any]]" = OrderedDict()

        def ensure(lot_entry_id: int, *, batch_id: int | None, gross: int = 0) -> dict[str, Any]:
            lot = lots.get(lot_entry_id)
            if lot is None:
                lot = {"lot_entry_id": lot_entry_id, "batch_id": batch_id, "gross": 0, "available": 0, "committed": 0}
                lots[lot_entry_id] = lot
            return lot

        def take_available(lot_entry_id: int, amount: int) -> None:
            lot = lots.get(lot_entry_id)
            if lot is None or lot["available"] < amount:
                raise ConflictError("台账资金来源追踪失配：可用余额不足")
            lot["available"] -= amount

        def take_committed(lot_entry_id: int, amount: int) -> None:
            lot = lots.get(lot_entry_id)
            if lot is None or lot["committed"] < amount:
                raise ConflictError("台账资金来源追踪失配：承诺余额不足")
            lot["committed"] -= amount

        for row in rows:
            event = row["event_type"]
            amount = int(row["amount_cents"])
            if event == "donation_in":
                lot = ensure(int(row["id"]), batch_id=row["batch_id"])
                lot["gross"] += amount
                lot["available"] += amount
            elif event == "transfer_in":
                lot = ensure(int(row["id"]), batch_id=row["batch_id"])
                lot["gross"] += amount
                lot["available"] += amount
                # 保留跨用途调拨的原始捐赠来源，供资金来源追踪穿透展示
                lot["provenance"] = [
                    {"batch_id": item.get("batch_id"), "lot_entry_id": int(item["lot_entry_id"]),
                     "amount_cents": int(item["amount_cents"])}
                    for item in json.loads(row["allocation_json"])
                ]
            elif event in {"donation_reversal", "transfer_out"}:
                for item in json.loads(row["allocation_json"]):
                    take_available(int(item["lot_entry_id"]), int(item["amount_cents"]))
            elif event == "reservation_hold":
                for item in json.loads(row["allocation_json"]):
                    lot_id = int(item["lot_entry_id"])
                    take_available(lot_id, int(item["amount_cents"]))
                    lots[lot_id]["committed"] += int(item["amount_cents"])
            elif event == "reservation_release":
                for item in json.loads(row["allocation_json"]):
                    lot_id = int(item["lot_entry_id"])
                    take_committed(lot_id, int(item["amount_cents"]))
                    lots[lot_id]["available"] += int(item["amount_cents"])
            elif event == "expenditure":
                for item in json.loads(row["allocation_json"]):
                    take_committed(int(item["lot_entry_id"]), int(item["amount_cents"]))
        return lots

    def _consume_lots(
        self,
        connection: sqlite3.Connection,
        purpose_id: int,
        amount: int,
        *,
        lot_filter: Any = None,
    ) -> list[dict[str, int]]:
        """按入账顺序（FIFO）从来源批次的可用余额中扣减整数分。"""
        lots = self._replay_lots(connection, purpose_id)
        allocation: list[dict[str, int]] = []
        remaining = amount
        for lot in lots.values():
            if lot["available"] <= 0 or (lot_filter is not None and not lot_filter(lot)):
                continue
            take = min(lot["available"], remaining)
            if take <= 0:
                continue
            allocation.append({
                "lot_entry_id": lot["lot_entry_id"],
                "batch_id": lot["batch_id"],
                "amount_cents": take,
            })
            remaining -= take
            if remaining == 0:
                break
        if remaining != 0:
            available = sum(
                lot["available"] for lot in lots.values()
                if lot_filter is None or lot_filter(lot)
            )
            raise ConflictError(
                "可支配资金不足",
                context={"available": yuan(available), "requested": yuan(amount)},
            )
        return allocation

    def _hold_open_cells(self, connection: sqlite3.Connection, hold_entry_id: int) -> list[dict[str, Any]]:
        """单笔承诺下各来源批次尚未支出/释放的整数分余额。"""
        hold = connection.execute("SELECT * FROM ledger_entries WHERE id=?", (hold_entry_id,)).fetchone()
        if hold is None:
            raise NotFoundError("预算承诺记录不存在")
        cells: dict[int, dict[str, Any]] = {}
        for item in json.loads(hold["allocation_json"]):
            lot_id = int(item["lot_entry_id"])
            cells[lot_id] = {
                "lot_entry_id": lot_id,
                "batch_id": item["batch_id"],
                "remaining": int(item["amount_cents"]),
            }
        rows = connection.execute(
            "SELECT event_type,allocation_json FROM ledger_entries WHERE related_entry_id=? "
            "AND event_type IN ('reservation_release','expenditure') ORDER BY id",
            (hold_entry_id,),
        ).fetchall()
        for row in rows:
            for item in json.loads(row["allocation_json"]):
                if int(item.get("hold_entry_id", hold_entry_id)) != hold_entry_id:
                    continue
                lot_id = int(item["lot_entry_id"])
                cell = cells.get(lot_id)
                if cell is None or cell["remaining"] < int(item["amount_cents"]):
                    raise ConflictError("台账资金来源追踪失配：承诺单元余额不足")
                cell["remaining"] -= int(item["amount_cents"])
        return [cell for cell in cells.values() if cell["remaining"] > 0]

    def _consume_hold_cells(
        self, connection: sqlite3.Connection, campaign_id: int, amount: int
    ) -> tuple[list[dict[str, int]], int]:
        holds = connection.execute(
            "SELECT id FROM ledger_entries WHERE campaign_id=? AND event_type='reservation_hold' ORDER BY id",
            (campaign_id,),
        ).fetchall()
        cells: list[dict[str, Any]] = []
        for hold in holds:
            for cell in self._hold_open_cells(connection, int(hold["id"])):
                cells.append({"hold_entry_id": int(hold["id"]), **cell})
        return self._take_cells(cells, amount)

    @staticmethod
    def _take_cells(cells: list[dict[str, Any]], amount: int) -> tuple[list[dict[str, int]], int]:
        allocation: list[dict[str, int]] = []
        remaining = amount
        first_hold: int | None = None
        for cell in cells:
            if remaining == 0:
                break
            take = min(int(cell["remaining"]), remaining)
            allocation.append({
                "hold_entry_id": int(cell["hold_entry_id"]),
                "lot_entry_id": int(cell["lot_entry_id"]),
                "batch_id": cell["batch_id"],
                "amount_cents": take,
            })
            first_hold = first_hold if first_hold is not None else int(cell["hold_entry_id"])
            remaining -= take
        if remaining != 0:
            raise ConflictError("预算承诺可用余额不足")
        return allocation, int(first_hold)  # type: ignore[arg-type]

    # ================================================================ 内部：基础

    def _campaign(self, code: str, temple_code: str | None = None) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM restoration_campaigns WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("修缮计划不存在")
        if temple_code:
            temple = self.connection.execute("SELECT id FROM temple_sites WHERE code=?", (temple_code,)).fetchone()
            if temple is None or int(temple["id"]) != int(row["temple_id"]):
                raise NotFoundError("修缮计划不属于该寺院")
        return row

    @staticmethod
    def _amount(value: Any, label: str) -> int:
        try:
            return to_cents(value)
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError(f"{label}：{exc}") from exc

    @staticmethod
    def _require_credential(credential: Any) -> str:
        if not isinstance(credential, str) or len(credential.strip()) < 4:
            raise ValidationError("业务凭据号不能为空且至少 4 个字符")
        return credential_fingerprint(credential)

    @staticmethod
    def _register_credential(
        connection: sqlite3.Connection, fingerprint: str, scope: str, ref_id: int, now: str
    ) -> None:
        try:
            connection.execute(
                "INSERT INTO ledger_credential_records(credential_hash,scope,ref_type,ref_id,created_at) VALUES(?,?,?,?,?)",
                (fingerprint, scope, scope, ref_id, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一业务凭据已登记，禁止重复入账") from exc

    def _insert_entry(
        self,
        connection: sqlite3.Connection,
        *,
        purpose_id: int,
        event_type: str,
        amount: int,
        actor: str,
        memo: str,
        created_at: str,
        batch_id: int | None = None,
        campaign_id: int | None = None,
        related_entry_id: int | None = None,
        transfer_request_id: int | None = None,
        allocation: list[dict[str, int]] | None = None,
    ) -> int:
        if amount < 0:
            raise ValidationError("台账金额不能为负")
        balance_sign, committed_sign = ENTRY_EFFECTS[event_type]
        direction = "in" if (balance_sign > 0 or (balance_sign == 0 and committed_sign < 0)) else "out"
        cursor = connection.execute(
            "INSERT INTO ledger_entries(purpose_id,batch_id,event_type,direction,amount_cents,balance_delta_cents,"
            "committed_delta_cents,related_entry_id,campaign_id,transfer_request_id,actor,memo,allocation_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                purpose_id, batch_id, event_type, direction, amount,
                balance_sign * amount, committed_sign * amount,
                related_entry_id, campaign_id, transfer_request_id,
                actor, memo,
                json.dumps(allocation or [], ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _totals(connection: sqlite3.Connection, purpose_id: int, at: str | None) -> tuple[int, int]:
        sql = (
            "SELECT COALESCE(SUM(balance_delta_cents),0),COALESCE(SUM(committed_delta_cents),0) "
            "FROM ledger_entries WHERE purpose_id=?"
        )
        params: list[Any] = [purpose_id]
        if at:
            sql += " AND created_at<=?"
            params.append(at)
        row = connection.execute(sql, params).fetchone()
        return int(row[0]), int(row[1])

    def _held_open(self, connection: sqlite3.Connection, campaign_id: int) -> int:
        held = int(connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries "
            "WHERE campaign_id=? AND event_type='reservation_hold'",
            (campaign_id,),
        ).fetchone()[0])
        released = int(connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries "
            "WHERE campaign_id=? AND event_type='reservation_release'",
            (campaign_id,),
        ).fetchone()[0])
        return held - released

    def _sum_event(self, event_type: str, purpose_id: int, campaign_id: int, *, at: str | None) -> int:
        sql = (
            "SELECT COALESCE(SUM(amount_cents),0) FROM ledger_entries "
            "WHERE purpose_id=? AND event_type=? AND campaign_id=?"
        )
        params: list[Any] = [purpose_id, event_type, campaign_id]
        if at:
            sql += " AND created_at<=?"
            params.append(at)
        return int(self.connection.execute(sql, params).fetchone()[0])

    @staticmethod
    def _point_in_time(at: str | None) -> str | None:
        if not at:
            return None
        try:
            return to_storage(from_storage(at))
        except (TypeError, ValueError) as exc:
            raise ValidationError("查询时点格式不正确") from exc

    @staticmethod
    def _optional_time(value: str | None, label: str) -> str | None:
        if not value:
            return None
        try:
            return to_storage(from_storage(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    def _assert_invariants(self, connection: sqlite3.Connection, purpose_ids: set[int]) -> None:
        for purpose_id in purpose_ids:
            balance, committed = self._totals(connection, purpose_id, None)
            if balance < 0:
                raise ConflictError("台账不变量校验失败：账面余额为负")
            if committed < 0:
                raise ConflictError("台账不变量校验失败：已承诺金额为负")
            if committed > balance:
                raise ConflictError("台账不变量校验失败：已承诺超过账面余额")
            lots = self._replay_lots(connection, purpose_id)
            lot_balance = sum(lot["available"] + lot["committed"] for lot in lots.values())
            lot_committed = sum(lot["committed"] for lot in lots.values())
            if lot_balance != balance or lot_committed != committed:
                raise ConflictError("台账不变量校验失败：余额与资金来源流水不匹配")

    @staticmethod
    def _entry_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["amount"] = yuan(result.pop("amount_cents"))
        result["balance_delta"] = yuan(result.pop("balance_delta_cents"))
        result["committed_delta"] = yuan(result.pop("committed_delta_cents"))
        allocation: list[dict[str, Any]] = []
        for item in json.loads(result.pop("allocation_json")):
            allocation.append({**{key: int(value) for key, value in item.items()}, "amount": yuan(int(item["amount_cents"]))})
        result["allocation"] = allocation
        return result
