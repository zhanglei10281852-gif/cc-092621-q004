from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.temple.schema import ensure_temple_schema

CENTS = 100
MAX_AMOUNT_MINOR = 1_000_000_000_000


def parse_amount_minor(value: str | int) -> int:
    if isinstance(value, int):
        minor = value
    else:
        try:
            decimal = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError("金额格式不正确") from exc
        if decimal.as_tuple().exponent < -2:
            raise ValidationError("金额最小单位为分")
        if not decimal.is_finite():
            raise ValidationError("金额格式不正确")
        minor = int(decimal.scaleb(2).to_integral_value())
    if minor <= 0:
        raise ValidationError("金额必须大于零")
    if minor > MAX_AMOUNT_MINOR:
        raise ValidationError("金额超出允许范围")
    return minor


def format_minor(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(int(value))
    return f"{sign}{value // CENTS}.{value % CENTS:02d}"


def donor_digest(donor_ref: str) -> str:
    normalized = donor_ref.strip().casefold()
    return hashlib.sha256(f"donor:{normalized}".encode("utf-8")).hexdigest()


def _parse_time(value: str, label: str) -> str:
    try:
        return to_storage(from_storage(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}格式不正确") from exc


class FundLedgerService:
    """专项资金台账：捐赠批次、用途限制、承诺/支出/释放与跨用途调拨。

    所有余额均为 fund_ledger_entries 流水的视图求和，台账本身只追加，
    因此服务重启后可以按任一时点复算出完全相同的结果。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 资金用途

    def create_fund(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        restriction_type = payload["restriction_type"]
        hall_id = None
        component_code = ""
        if restriction_type == "hall":
            hall_id = self._hall_id(temple["id"], payload.get("hall_code"))
        elif restriction_type == "component":
            component_code = (payload.get("component_code") or "").strip()
            if not component_code:
                raise ValidationError("构件类用途必须提供构件编码")
            if payload.get("hall_code"):
                hall_id = self._hall_id(temple["id"], payload["hall_code"])
        elif payload.get("hall_code") or payload.get("component_code"):
            raise ValidationError("未限定用途的专项资金不能绑定殿堂或构件")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO restoration_funds(temple_id,code,name,restriction_type,hall_id,component_code,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], restriction_type, hall_id, component_code, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("专项资金编码已存在") from exc
            self._event(connection, "fund", cursor.lastrowid, "created", payload["actor"], {"restriction_type": restriction_type}, now)
            return self.fund_detail(cursor.lastrowid, connection=connection)

    def list_funds(self, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        if state:
            clauses.append("f.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT f.*,n.code AS temple_code,n.name AS temple_name,s.code AS hall_code "
            "FROM restoration_funds f JOIN temple_sites n ON n.id=f.temple_id "
            "LEFT JOIN worship_halls s ON s.id=f.hall_id" + where + " ORDER BY f.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def fund_detail(self, fund_code_or_id: str | int, *, as_of: str | None = None, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        fund = self._fund(fund_code_or_id, connection)
        point = _parse_time(as_of, "时点") if as_of else None
        balances = self._balances(connection, fund["id"], point)
        result = dict(fund)
        result.pop("temple_id", None)
        result["temple"] = {
            "id": fund["temple_id"],
            "code": connection.execute("SELECT code FROM temple_sites WHERE id=?", (fund["temple_id"],)).fetchone()[0],
        }
        if fund["hall_id"]:
            hall = connection.execute("SELECT code,name FROM worship_halls WHERE id=?", (fund["hall_id"],)).fetchone()
            result["hall"] = {"code": hall["code"], "name": hall["name"]} if hall else None
        else:
            result["hall"] = None
        result.update(balances)
        result["as_of"] = point
        return result

    def fund_ledger(self, fund_code_or_id: str | int, *, as_of: str | None = None, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        fund = self._fund(fund_code_or_id)
        point = _parse_time(as_of, "时点") if as_of else None
        sql = "SELECT * FROM fund_ledger_entries WHERE fund_id=? AND id>? "
        params: list[Any] = [fund["id"], after_id]
        if point:
            sql += "AND created_at<=? "
            params.append(point)
        sql += "ORDER BY id LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return {"items": [self._entry_json(row) for row in rows]}

    def fund_sources(self, fund_code_or_id: str | int, *, as_of: str | None = None) -> dict[str, Any]:
        fund = self._fund(fund_code_or_id)
        point = _parse_time(as_of, "时点") if as_of else None
        return {"items": self._source_rows(self.connection, fund["id"], point)}

    # ------------------------------------------------------------------ 捐赠与撤销

    def record_donation(self, payload: dict[str, Any]) -> dict[str, Any]:
        fund = self._fund(payload["fund_code"])
        if fund["state"] != "active":
            raise ConflictError("专项资金已结清，不能再接收捐赠")
        amount_minor = parse_amount_minor(payload["amount"])
        received_at = _parse_time(payload["received_at"], "到账时间")
        donor_hash = donor_digest(payload["donor_ref"])
        now = to_storage(self.clock.now())
        command_key = f"donation:{payload['receipt_no']}"
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM donation_batches WHERE receipt_no=?", (payload["receipt_no"],)).fetchone()
            if existing is not None:
                if (
                    existing["fund_id"] != fund["id"]
                    or existing["amount_minor"] != amount_minor
                    or existing["batch_code"] != payload["batch_code"]
                    or existing["donor_hash"] != donor_hash
                ):
                    raise ConflictError("同一凭据对应了不同的捐赠内容")
                return {**self._batch_json(connection, existing["id"]), "replayed": True}
            try:
                cursor = connection.execute(
                    "INSERT INTO donation_batches(fund_id,batch_code,receipt_no,donor_hash,channel,amount_minor,received_at,note,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (fund["id"], payload["batch_code"], payload["receipt_no"], donor_hash, payload.get("channel", ""), amount_minor, received_at, payload.get("note", ""), payload["actor"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("捐赠批次或凭据已存在") from exc
            batch_id = cursor.lastrowid
            entry_id = self._insert_entry(
                connection,
                fund_id=fund["id"],
                entry_type="donation",
                amount_minor=amount_minor,
                command_key=command_key,
                donation_batch_id=batch_id,
                actor=payload["actor"],
                reason=payload.get("note", ""),
                created_at=received_at,
            )
            self._event(connection, "fund", fund["id"], "donation_recorded", payload["actor"], {"batch_code": payload["batch_code"], "amount_minor": amount_minor, "ledger_entry_id": entry_id}, now)
            return {**self._batch_json(connection, batch_id), "replayed": False}

    def reverse_donation(self, payload: dict[str, Any]) -> dict[str, Any]:
        amount_minor = parse_amount_minor(payload["amount"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            batch = self._batch_by_receipt(connection, payload["receipt_no"])
            replayed = self._replay_command(connection, payload["command_key"])
            if replayed is not None:
                if replayed["donation_batch_id"] != batch["id"] or replayed["entry_type"] != "reversal" or replayed["amount_minor"] != amount_minor:
                    raise ConflictError("同一凭据对应了不同的撤销请求")
                return {**self._batch_json(connection, batch["id"]), "replayed": True}
            self._ensure_command_unused(connection, payload["command_key"])
            unconsumed = batch["amount_minor"] - batch["reversed_minor"] - self._batch_consumed(connection, batch["id"])
            if amount_minor > unconsumed:
                raise ConflictError("只能撤销尚未支出或调出的捐赠金额", context={"remaining_minor": unconsumed})
            balances = self._balances(connection, batch["fund_id"])
            if amount_minor > balances["available_minor"]:
                raise ConflictError("捐赠撤销后可支配金额将为负，不能撤销", context={"available_minor": balances["available_minor"]})
            connection.execute(
                "UPDATE donation_batches SET reversed_minor=reversed_minor+?,state=CASE WHEN reversed_minor+?=amount_minor THEN 'reversed' ELSE state END WHERE id=?",
                (amount_minor, amount_minor, batch["id"]),
            )
            self._insert_entry(
                connection,
                fund_id=batch["fund_id"],
                entry_type="reversal",
                amount_minor=amount_minor,
                command_key=payload["command_key"],
                donation_batch_id=batch["id"],
                actor=payload["actor"],
                reason=payload["reason"],
                created_at=now,
            )
            self._event(connection, "fund", batch["fund_id"], "donation_reversed", payload["actor"], {"receipt_no": batch["receipt_no"], "amount_minor": amount_minor}, now)
            return {**self._batch_json(connection, batch["id"]), "replayed": False}

    # ------------------------------------------------------------------ 预算承诺/支出/释放

    def commit_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self._campaign_by_code(self.connection, payload["campaign_code"])
        fund = self._fund(payload["fund_code"])
        if fund["temple_id"] != campaign["temple_id"]:
            raise ValidationError("专项资金与修缮计划不属于同一寺院")
        if campaign["state"] in {"completed", "cancelled"}:
            raise ConflictError("修缮计划已结束，不能再承诺预算")
        if fund["hall_id"] is not None:
            target_halls = {
                int(row["hall_id"])
                for row in self.connection.execute(
                    "SELECT hall_id FROM restoration_targets WHERE restoration_campaign_id=? AND hall_id IS NOT NULL",
                    (campaign["id"],),
                ).fetchall()
            }
            if target_halls and fund["hall_id"] not in target_halls:
                raise ConflictError("殿堂限定用途的资金不能承诺给不包含该殿堂的修缮计划")
        amount_minor = parse_amount_minor(payload["amount"])
        now = to_storage(self.clock.now())
        command_key = f"commit:{payload['code']}"
        with transaction(immediate=True) as inner:
            existing = inner.execute("SELECT * FROM budget_commitments WHERE code=?", (payload["code"],)).fetchone()
            if existing is not None:
                if existing["fund_id"] != fund["id"] or existing["campaign_id"] != campaign["id"] or existing["amount_minor"] != amount_minor:
                    raise ConflictError("同一承诺编码对应了不同的承诺内容")
                return {**self._commitment_json(inner, existing["id"]), "replayed": True}
            balances = self._balances(inner, fund["id"])
            if amount_minor > balances["available_minor"]:
                raise ConflictError("专项资金可支配金额不足", context={"available_minor": balances["available_minor"]})
            try:
                cursor = inner.execute(
                    "INSERT INTO budget_commitments(campaign_id,fund_id,code,amount_minor,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (campaign["id"], fund["id"], payload["code"], amount_minor, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("预算承诺编码已存在") from exc
            commitment_id = cursor.lastrowid
            self._insert_entry(
                inner,
                fund_id=fund["id"],
                entry_type="commitment",
                amount_minor=amount_minor,
                command_key=command_key,
                commitment_id=commitment_id,
                campaign_id=campaign["id"],
                actor=payload["actor"],
                reason=payload.get("reason", ""),
                created_at=now,
            )
            self._event(inner, "budget_commitment", commitment_id, "committed", payload["actor"], {"amount_minor": amount_minor, "campaign_code": campaign["code"]}, now)
            return {**self._commitment_json(inner, commitment_id), "replayed": False}

    def record_expenditure(self, commitment_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        amount_minor = parse_amount_minor(payload["amount"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            commitment = self._commitment_by_code(connection, commitment_code)
            replayed = self._replay_command(connection, payload["command_key"])
            if replayed is not None:
                if replayed["commitment_id"] != commitment["id"] or replayed["entry_type"] != "expenditure" or replayed["amount_minor"] != amount_minor:
                    raise ConflictError("同一凭据对应了不同的支出请求")
                return {**self._commitment_json(connection, commitment["id"]), "replayed": True}
            self._ensure_command_unused(connection, payload["command_key"])
            held = commitment["amount_minor"] - commitment["spent_minor"] - commitment["released_minor"]
            if amount_minor > held:
                raise ConflictError("支出超过承诺余额", context={"held_minor": held})
            entry_id = self._insert_entry(
                connection,
                fund_id=commitment["fund_id"],
                entry_type="expenditure",
                amount_minor=amount_minor,
                command_key=payload["command_key"],
                commitment_id=commitment["id"],
                campaign_id=commitment["campaign_id"],
                actor=payload["actor"],
                reason=payload["reason"],
                created_at=now,
            )
            self._consume_sources(connection, commitment["fund_id"], entry_id, amount_minor, now)
            connection.execute(
                "UPDATE budget_commitments SET spent_minor=spent_minor+?,updated_at=? WHERE id=?",
                (amount_minor, now, commitment["id"]),
            )
            self._refresh_commitment_state(connection, commitment["id"], now)
            self._event(connection, "budget_commitment", commitment["id"], "expenditure_recorded", payload["actor"], {"amount_minor": amount_minor, "ledger_entry_id": entry_id}, now)
            return {**self._commitment_json(connection, commitment["id"]), "replayed": False}

    def release_commitment(self, commitment_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        amount_minor = parse_amount_minor(payload["amount"]) if payload.get("amount") else None
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            commitment = self._commitment_by_code(connection, commitment_code)
            replayed = self._replay_command(connection, payload["command_key"])
            if replayed is not None:
                if replayed["commitment_id"] != commitment["id"] or replayed["entry_type"] != "commitment_release" or (amount_minor is not None and replayed["amount_minor"] != amount_minor):
                    raise ConflictError("同一凭据对应了不同的释放请求")
                return {**self._commitment_json(connection, commitment["id"]), "replayed": True}
            self._ensure_command_unused(connection, payload["command_key"])
            held = commitment["amount_minor"] - commitment["spent_minor"] - commitment["released_minor"]
            target = held if amount_minor is None else amount_minor
            if target <= 0:
                raise ConflictError("承诺没有可释放的占用金额")
            if target > held:
                raise ConflictError("释放金额超过承诺占用余额", context={"held_minor": held})
            self._insert_entry(
                connection,
                fund_id=commitment["fund_id"],
                entry_type="commitment_release",
                amount_minor=target,
                command_key=payload["command_key"],
                commitment_id=commitment["id"],
                campaign_id=commitment["campaign_id"],
                actor=payload["actor"],
                reason=payload["reason"],
                created_at=now,
            )
            connection.execute(
                "UPDATE budget_commitments SET released_minor=released_minor+?,updated_at=? WHERE id=?",
                (target, now, commitment["id"]),
            )
            self._refresh_commitment_state(connection, commitment["id"], now)
            self._event(connection, "budget_commitment", commitment["id"], "commitment_released", payload["actor"], {"amount_minor": target}, now)
            return {**self._commitment_json(connection, commitment["id"]), "replayed": False}

    def release_campaign_commitments(
        self,
        connection: sqlite3.Connection,
        campaign_id: int,
        actor: str,
        reason: str,
        now: str,
    ) -> list[int]:
        """项目取消时在调用方事务内释放其全部未结清承诺占用。"""
        rows = connection.execute(
            "SELECT * FROM budget_commitments WHERE campaign_id=? AND (amount_minor-spent_minor-released_minor)>0 ORDER BY id",
            (campaign_id,),
        ).fetchall()
        released_ids: list[int] = []
        for commitment in rows:
            held = commitment["amount_minor"] - commitment["spent_minor"] - commitment["released_minor"]
            command_key = f"release:campaign:{campaign_id}:commitment:{commitment['id']}"
            self._insert_entry(
                connection,
                fund_id=commitment["fund_id"],
                entry_type="commitment_release",
                amount_minor=held,
                command_key=command_key,
                commitment_id=commitment["id"],
                campaign_id=campaign_id,
                actor=actor,
                reason=reason,
                created_at=now,
            )
            connection.execute(
                "UPDATE budget_commitments SET released_minor=released_minor+?,updated_at=? WHERE id=?",
                (held, now, commitment["id"]),
            )
            self._refresh_commitment_state(connection, commitment["id"], now)
            self._event(connection, "budget_commitment", commitment["id"], "commitment_released", actor, {"amount_minor": held, "reason": "campaign_cancelled"}, now)
            released_ids.append(commitment["id"])
        return released_ids

    def campaign_finance(self, campaign_code_or_id: str | int) -> dict[str, Any]:
        campaign = self._campaign_by_code_or_id(self.connection, campaign_code_or_id)
        commitments = self.connection.execute("SELECT * FROM budget_commitments WHERE campaign_id=? ORDER BY id", (campaign["id"],)).fetchall()
        entries = self.connection.execute("SELECT * FROM fund_ledger_entries WHERE campaign_id=? ORDER BY id", (campaign["id"],)).fetchall()
        committed = sum(int(row["amount_minor"]) for row in commitments)
        spent = sum(int(row["spent_minor"]) for row in commitments)
        released = sum(int(row["released_minor"]) for row in commitments)
        return {
            "campaign": dict(campaign),
            "committed_minor": committed,
            "spent_minor": spent,
            "released_minor": released,
            "held_minor": committed - spent - released,
            "commitments": [self._commitment_json(self.connection, row["id"]) for row in commitments],
            "ledger": [self._entry_json(row) for row in entries],
        }

    # ------------------------------------------------------------------ 跨用途调拨（双重确认）

    def propose_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = self._fund(payload["from_fund_code"])
        target = self._fund(payload["to_fund_code"])
        if source["id"] == target["id"]:
            raise ValidationError("调出与调入专项资金不能相同")
        proposed_by = payload["proposed_by"].strip()
        required_approver = payload["required_approver"].strip()
        if proposed_by.casefold() == required_approver.casefold():
            raise ConflictError("跨用途调拨的提案人与批准人不能为同一人")
        amount_minor = parse_amount_minor(payload["amount"])
        balances = self._balances(self.connection, source["id"])
        if amount_minor > balances["available_minor"]:
            raise ConflictError("调出专项资金可支配金额不足", context={"available_minor": balances["available_minor"]})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            self._ensure_command_unused(connection, payload["command_key"])
            try:
                cursor = connection.execute(
                    "INSERT INTO fund_transfer_orders(code,command_key,from_fund_id,to_fund_id,amount_minor,reason,proposed_by,required_approver,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (payload["code"], payload["command_key"], source["id"], target["id"], amount_minor, payload["reason"], proposed_by, required_approver, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("调拨单编码或凭据已存在") from exc
            order_id = cursor.lastrowid
            self._event(connection, "transfer_order", order_id, "proposed", proposed_by, {"code": payload["code"], "amount_minor": amount_minor}, now)
            return self._transfer_json(connection, order_id)

    def confirm_transfer(self, order_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        approver = payload["approver"].strip()
        now = to_storage(self.clock.now())
        group = f"transfer:{order_code}"
        with transaction(immediate=True) as connection:
            order = self._transfer_by_code(connection, order_code)
            replayed_entry = self._replay_command(connection, payload["command_key"])
            if replayed_entry is not None:
                if replayed_entry["entry_type"] != "transfer_out" or replayed_entry["transfer_group_code"] != group:
                    raise ConflictError("同一凭据对应了不同的调拨确认")
                return {**self._transfer_json(connection, order["id"]), "replayed": True}
            if order["state"] != "proposed":
                raise ConflictError("只有待批准的调拨可以确认", context={"state": order["state"]})
            if approver.casefold() != order["required_approver"].strip().casefold():
                raise PermissionDecisionError("调拨必须由指定批准人确认")
            if approver.casefold() == order["proposed_by"].strip().casefold():
                raise PermissionDecisionError("调拨提案人不能同时作为批准人")
            balances = self._balances(connection, order["from_fund_id"])
            if order["amount_minor"] > balances["available_minor"]:
                raise ConflictError("调出专项资金可支配金额不足，调拨不能确认", context={"available_minor": balances["available_minor"]})
            self._ensure_command_unused(connection, payload["command_key"])
            out_id = self._insert_entry(
                connection,
                fund_id=order["from_fund_id"],
                entry_type="transfer_out",
                amount_minor=order["amount_minor"],
                command_key=payload["command_key"],
                counterparty_fund_id=order["to_fund_id"],
                transfer_group_code=group,
                actor=approver,
                reason=order["reason"],
                created_at=now,
            )
            self._consume_sources(connection, order["from_fund_id"], out_id, order["amount_minor"], now)
            in_id = self._insert_entry(
                connection,
                fund_id=order["to_fund_id"],
                entry_type="transfer_in",
                amount_minor=order["amount_minor"],
                command_key=None,
                counterparty_fund_id=order["from_fund_id"],
                transfer_group_code=group,
                linked_entry_id=out_id,
                actor=approver,
                reason=order["reason"],
                created_at=now,
            )
            connection.execute("UPDATE fund_ledger_entries SET linked_entry_id=? WHERE id=?", (in_id, out_id))
            connection.execute(
                "UPDATE fund_transfer_orders SET state='confirmed',confirmed_by=?,decided_at=? WHERE id=?",
                (approver, now, order["id"]),
            )
            self._event(connection, "transfer_order", order["id"], "confirmed", approver, {"out_entry_id": out_id, "in_entry_id": in_id}, now)
            return {**self._transfer_json(connection, order["id"]), "replayed": False}

    def reject_transfer(self, order_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        approver = payload["approver"].strip()
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            order = self._transfer_by_code(connection, order_code)
            if order["state"] != "proposed":
                raise ConflictError("只有待批准的调拨可以驳回", context={"state": order["state"]})
            allowed = {order["required_approver"].strip().casefold(), order["proposed_by"].strip().casefold()}
            if approver.casefold() not in allowed:
                raise PermissionDecisionError("只有提案人或指定批准人可以驳回调拨")
            connection.execute(
                "UPDATE fund_transfer_orders SET state='rejected',rejected_by=?,decided_at=? WHERE id=?",
                (approver, now, order["id"]),
            )
            self._event(connection, "transfer_order", order["id"], "rejected", approver, {"reason": payload.get("reason", "")}, now)
            return self._transfer_json(connection, order["id"])

    def transfer_detail(self, order_code: str) -> dict[str, Any]:
        return self._transfer_json(self.connection, self._transfer_by_code(self.connection, order_code)["id"])

    # ------------------------------------------------------------------ 一致性校验

    def verify_integrity(self, *, as_of: str | None = None) -> dict[str, Any]:
        point = _parse_time(as_of, "时点") if as_of else None
        connection = self.connection
        funds = connection.execute("SELECT id,code FROM restoration_funds ORDER BY id").fetchall()
        mismatches: list[dict[str, Any]] = []
        fund_reports: list[dict[str, Any]] = []
        for fund in funds:
            balances = self._balances(connection, fund["id"], point)
            sources = self._source_rows(connection, fund["id"], point)
            sources_remaining = sum(int(item["remaining_minor"]) for item in sources)
            committed_ledger = balances["committed_minor"]
            committed_contracts = self._held_commitments(connection, fund["id"], point)
            report = {
                "fund_code": fund["code"],
                "book_balance_minor": balances["book_balance_minor"],
                "committed_minor": committed_ledger,
                "available_minor": balances["available_minor"],
                "sources_remaining_minor": sources_remaining,
                "commitments_held_minor": committed_contracts,
            }
            fund_reports.append(report)
            if sources_remaining != balances["book_balance_minor"]:
                mismatches.append({"fund_code": fund["code"], "kind": "book_vs_sources", **report})
            if committed_ledger != committed_contracts:
                mismatches.append({"fund_code": fund["code"], "kind": "committed_vs_commitments", **report})
            if balances["available_minor"] < 0:
                mismatches.append({"fund_code": fund["code"], "kind": "negative_available", **report})
        broken_allocations = connection.execute(
            "SELECT a.ledger_entry_id,e.entry_type,SUM(a.amount_minor) AS allocated,e.amount_minor "
            "FROM fund_source_allocations a JOIN fund_ledger_entries e ON e.id=a.ledger_entry_id "
            + ("WHERE e.created_at<=? " if point else "")
            + "GROUP BY a.ledger_entry_id HAVING allocated<>e.amount_minor OR e.entry_type NOT IN ('expenditure','transfer_out')",
            (point,) if point else (),
        ).fetchall()
        for row in broken_allocations:
            mismatches.append({"kind": "allocation_mismatch", "ledger_entry_id": row["ledger_entry_id"], "entry_type": row["entry_type"], "allocated_minor": int(row["allocated"]), "amount_minor": int(row["amount_minor"])})
        return {"consistent": not mismatches, "as_of": point, "funds": fund_reports, "mismatches": mismatches}

    # ------------------------------------------------------------------ 内部 helpers

    def _insert_entry(self, connection: sqlite3.Connection, **values: Any) -> int:
        keys = [
            "fund_id", "entry_type", "amount_minor", "command_key", "donation_batch_id", "commitment_id",
            "counterparty_fund_id", "transfer_group_code", "linked_entry_id", "campaign_id", "actor", "reason", "created_at",
        ]
        ordered = [values.get(key) for key in keys]
        cursor = connection.execute(
            f"INSERT INTO fund_ledger_entries({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
            ordered,
        )
        return int(cursor.lastrowid)

    def _consume_sources(self, connection: sqlite3.Connection, fund_id: int, ledger_entry_id: int, amount_minor: int, now: str) -> None:
        """按入账时间 FIFO 核销资金来源桶，使每笔支出/调出都能回溯到原始捐赠。"""
        del now
        remaining = amount_minor
        for source in self._open_sources(connection, fund_id, None):
            if remaining <= 0:
                break
            take = min(remaining, source["remaining_minor"])
            connection.execute(
                "INSERT INTO fund_source_allocations(ledger_entry_id,donation_batch_id,source_entry_id,amount_minor) VALUES(?,?,?,?)",
                (ledger_entry_id, source["donation_batch_id"], source["transfer_entry_id"], take),
            )
            remaining -= take
        if remaining > 0:
            raise ConflictError("资金来源不足以覆盖本次动用")

    @staticmethod
    def _open_sources(connection: sqlite3.Connection, fund_id: int, point: str | None) -> list[sqlite3.Row]:
        time_filter = "AND e.created_at<=?" if point else ""
        rows = connection.execute(
            f"""
            SELECT kind, source_id, donation_batch_id, transfer_entry_id, sort_time,
                   inflow - consumed AS remaining_minor
            FROM (
                SELECT 'batch' AS kind, db.id AS source_id, db.id AS donation_batch_id, NULL AS transfer_entry_id,
                       db.received_at AS sort_time,
                       (SELECT COALESCE(SUM(CASE e.entry_type WHEN 'reversal' THEN -e.amount_minor ELSE e.amount_minor END),0)
                          FROM fund_ledger_entries e
                         WHERE e.donation_batch_id=db.id AND e.entry_type IN ('donation','reversal') {time_filter}) AS inflow,
                       (SELECT COALESCE(SUM(a.amount_minor),0) FROM fund_source_allocations a
                          JOIN fund_ledger_entries c ON c.id=a.ledger_entry_id
                         WHERE a.donation_batch_id=db.id {('AND c.created_at<=?' if point else '')}) AS consumed
                  FROM donation_batches db
                 WHERE db.fund_id=? {('AND db.received_at<=?' if point else '')}
                UNION ALL
                SELECT 'transfer' AS kind, e.id AS source_id, NULL AS donation_batch_id, e.id AS transfer_entry_id,
                       e.created_at AS sort_time,
                       e.amount_minor AS inflow,
                       (SELECT COALESCE(SUM(a.amount_minor),0) FROM fund_source_allocations a
                          JOIN fund_ledger_entries c ON c.id=a.ledger_entry_id
                         WHERE a.source_entry_id=e.id {('AND c.created_at<=?' if point else '')}) AS consumed
                  FROM fund_ledger_entries e
                 WHERE e.fund_id=? AND e.entry_type='transfer_in' {('AND e.created_at<=?' if point else '')}
            )
            WHERE inflow - consumed > 0
            ORDER BY sort_time, kind, source_id
            """,
            FundLedgerService._source_params(point, fund_id),
        ).fetchall()
        return list(rows)

    @staticmethod
    def _source_params(point: str | None, fund_id: int) -> list[Any]:
        if point is None:
            return [fund_id, fund_id]
        return [point, point, fund_id, point, point, fund_id, point]

    def _source_rows(self, connection: sqlite3.Connection, fund_id: int, point: str | None) -> list[dict[str, Any]]:
        rows = self._open_sources(connection, fund_id, point)
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["kind"] == "batch":
                batch = connection.execute(
                    "SELECT b.*,f.code AS fund_code FROM donation_batches b JOIN restoration_funds f ON f.id=b.fund_id WHERE b.id=?",
                    (row["donation_batch_id"],),
                ).fetchone()
                result.append({
                    "kind": "donation_batch",
                    "donation_batch_id": batch["id"],
                    "batch_code": batch["batch_code"],
                    "receipt_no": batch["receipt_no"],
                    "donor_hash": batch["donor_hash"],
                    "fund_code": batch["fund_code"],
                    "received_at": batch["received_at"],
                    "remaining_minor": int(row["remaining_minor"]),
                })
            else:
                entry = connection.execute(
                    "SELECT e.*,f.code AS fund_code,g.code AS counterparty_fund_code FROM fund_ledger_entries e "
                    "JOIN restoration_funds f ON f.id=e.fund_id "
                    "LEFT JOIN restoration_funds g ON g.id=e.counterparty_fund_id WHERE e.id=?",
                    (row["transfer_entry_id"],),
                ).fetchone()
                result.append({
                    "kind": "transfer_in",
                    "ledger_entry_id": entry["id"],
                    "transfer_group_code": entry["transfer_group_code"],
                    "fund_code": entry["fund_code"],
                    "counterparty_fund_code": entry["counterparty_fund_code"],
                    "actor": entry["actor"],
                    "received_at": entry["created_at"],
                    "remaining_minor": int(row["remaining_minor"]),
                })
        return result

    @staticmethod
    def _balances(connection: sqlite3.Connection, fund_id: int, point: str | None = None) -> dict[str, Any]:
        sql = (
            "SELECT COALESCE(SUM(book_delta),0) AS book_balance_minor,"
            "COALESCE(SUM(committed_delta),0) AS committed_minor,"
            "COALESCE(SUM(CASE WHEN entry_type='donation' THEN amount_minor ELSE 0 END),0) AS donated_minor,"
            "COALESCE(SUM(CASE WHEN entry_type='reversal' THEN amount_minor ELSE 0 END),0) AS reversed_minor,"
            "COALESCE(SUM(CASE WHEN entry_type='expenditure' THEN amount_minor ELSE 0 END),0) AS spent_minor,"
            "COALESCE(SUM(CASE WHEN entry_type='transfer_in' THEN amount_minor ELSE 0 END),0) AS transferred_in_minor,"
            "COALESCE(SUM(CASE WHEN entry_type='transfer_out' THEN amount_minor ELSE 0 END),0) AS transferred_out_minor "
            "FROM fund_ledger_movements WHERE fund_id=?"
        )
        params: list[Any] = [fund_id]
        if point:
            sql += " AND created_at<=?"
            params.append(point)
        row = connection.execute(sql, params).fetchone()
        result = {key: int(row[key]) for key in row.keys()}
        result["available_minor"] = result["book_balance_minor"] - result["committed_minor"]
        for key, value in list(result.items()):
            if key.endswith("_minor"):
                result[key.removesuffix("_minor")] = format_minor(value)
        return result

    @staticmethod
    def _held_commitments(connection: sqlite3.Connection, fund_id: int, point: str | None) -> int:
        # 时点口径：承诺以流水为准，承诺表只保存当前状态；时点复算由调用方与流水比对。
        if point:
            row = connection.execute(
                "SELECT COALESCE(SUM(committed_delta),0) FROM fund_ledger_movements WHERE fund_id=? AND created_at<=?",
                (fund_id, point),
            ).fetchone()
            return int(row[0])
        row = connection.execute(
            "SELECT COALESCE(SUM(amount_minor-spent_minor-released_minor),0) FROM budget_commitments WHERE fund_id=?",
            (fund_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _refresh_commitment_state(connection: sqlite3.Connection, commitment_id: int, now: str) -> None:
        row = connection.execute("SELECT amount_minor,spent_minor,released_minor FROM budget_commitments WHERE id=?", (commitment_id,)).fetchone()
        remaining = row["amount_minor"] - row["spent_minor"] - row["released_minor"]
        if remaining > 0:
            state = "partially_settled" if row["spent_minor"] > 0 else "held"
        else:
            state = "settled" if row["spent_minor"] > 0 else "released"
        connection.execute("UPDATE budget_commitments SET state=?,updated_at=? WHERE id=?", (state, now, commitment_id))

    @staticmethod
    def _batch_consumed(connection: sqlite3.Connection, batch_id: int) -> int:
        row = connection.execute("SELECT COALESCE(SUM(amount_minor),0) FROM fund_source_allocations WHERE donation_batch_id=?", (batch_id,)).fetchone()
        return int(row[0])

    def _batch_json(self, connection: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT b.*,f.code AS fund_code FROM donation_batches b JOIN restoration_funds f ON f.id=b.fund_id WHERE b.id=?",
            (batch_id,),
        ).fetchone()
        result = dict(row)
        result["remaining_minor"] = row["amount_minor"] - row["reversed_minor"] - self._batch_consumed(connection, batch_id)
        for key in ("amount_minor", "reversed_minor", "remaining_minor"):
            result[key.removesuffix("_minor")] = format_minor(result[key])
        return result

    def _commitment_json(self, connection: sqlite3.Connection, commitment_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT c.*,f.code AS fund_code,n.code AS campaign_code FROM budget_commitments c "
            "JOIN restoration_funds f ON f.id=c.fund_id "
            "JOIN restoration_campaigns n ON n.id=c.campaign_id WHERE c.id=?",
            (commitment_id,),
        ).fetchone()
        result = dict(row)
        result["held_minor"] = row["amount_minor"] - row["spent_minor"] - row["released_minor"]
        entries = connection.execute("SELECT * FROM fund_ledger_entries WHERE commitment_id=? ORDER BY id", (commitment_id,)).fetchall()
        result["entries"] = [self._entry_json(entry) for entry in entries]
        for key in ("amount_minor", "spent_minor", "released_minor", "held_minor"):
            result[key.removesuffix("_minor")] = format_minor(result[key])
        return result

    def _entry_json(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        allocations = self.connection.execute("SELECT * FROM fund_source_allocations WHERE ledger_entry_id=? ORDER BY id", (row["id"],)).fetchall()
        result["source_allocations"] = [dict(item) for item in allocations]
        result["amount"] = format_minor(row["amount_minor"])
        return result

    def _transfer_json(self, connection: sqlite3.Connection, order_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT o.*,f.code AS from_fund_code,t.code AS to_fund_code FROM fund_transfer_orders o "
            "JOIN restoration_funds f ON f.id=o.from_fund_id JOIN restoration_funds t ON t.id=o.to_fund_id WHERE o.id=?",
            (order_id,),
        ).fetchone()
        result = dict(row)
        entries = connection.execute("SELECT * FROM fund_ledger_entries WHERE transfer_group_code=? ORDER BY id", (f"transfer:{row['code']}",)).fetchall()
        result["entries"] = [self._entry_json(entry) for entry in entries]
        result["amount"] = format_minor(row["amount_minor"])
        return result

    @staticmethod
    def _replay_command(connection: sqlite3.Connection, command_key: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM fund_ledger_entries WHERE command_key=?", (command_key,)).fetchone()

    @staticmethod
    def _ensure_command_unused(connection: sqlite3.Connection, command_key: str) -> None:
        if connection.execute("SELECT 1 FROM fund_ledger_entries WHERE command_key=?", (command_key,)).fetchone():
            raise ConflictError("同一凭据已经入账，不能重复使用")

    def _fund(self, code_or_id: str | int, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        connection = connection or self.connection
        if isinstance(code_or_id, int) or (isinstance(code_or_id, str) and code_or_id.isdigit()):
            row = connection.execute("SELECT * FROM restoration_funds WHERE id=?", (int(code_or_id),)).fetchone()
        else:
            row = connection.execute("SELECT * FROM restoration_funds WHERE code=?", (code_or_id,)).fetchone()
        if row is None:
            raise NotFoundError("专项资金不存在")
        return row

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _hall_id(self, temple_id: int, hall_code: str | None) -> int:
        if not hall_code:
            raise ValidationError("殿堂类用途必须提供殿堂编码")
        row = self.connection.execute("SELECT id FROM worship_halls WHERE temple_id=? AND code=?", (temple_id, hall_code)).fetchone()
        if row is None:
            raise NotFoundError("用途殿堂不存在")
        return int(row["id"])

    def _batch_by_receipt(self, connection: sqlite3.Connection, receipt_no: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM donation_batches WHERE receipt_no=?", (receipt_no,)).fetchone()
        if row is None:
            raise NotFoundError("捐赠凭据不存在")
        return row

    def _commitment_by_code(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM budget_commitments WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("预算承诺不存在")
        return row

    def _campaign_by_code(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM restoration_campaigns WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("修缮计划不存在")
        return row

    def _campaign_by_code_or_id(self, connection: sqlite3.Connection, code_or_id: str | int) -> sqlite3.Row:
        if isinstance(code_or_id, int) or (isinstance(code_or_id, str) and code_or_id.isdigit()):
            row = connection.execute("SELECT * FROM restoration_campaigns WHERE id=?", (int(code_or_id),)).fetchone()
        else:
            row = connection.execute("SELECT * FROM restoration_campaigns WHERE code=?", (code_or_id,)).fetchone()
        if row is None:
            raise NotFoundError("修缮计划不存在")
        return row

    def _transfer_by_code(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM fund_transfer_orders WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("调拨单不存在")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO restoration_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (resource_type, resource_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )


class PermissionDecisionError(ConflictError):
    """调拨双重确认未通过：语义为冲突，保持 409 以便客户端按业务失败处理。"""

    code = "transfer_approval_invalid"
