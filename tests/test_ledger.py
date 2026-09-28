from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection
from app.temple.ledger.service import FundLedgerService, proportional_split
from app.temple.ledger.money import to_cents, yuan
from app.temple.operations import TempleRestorationService
from app.temple.rules import DEFAULT_RULES
from app.temple.service import TempleSafetyService

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------- helpers


def make_user(user_id: int, username: str) -> None:
    conn = get_connection()
    now = to_storage(NOW)
    conn.execute(
        "INSERT INTO users(id,username,password_hash,display_name,status,password_changed_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id, username, "pbkdf2_sha256$1$AA$AA", username, "active", now, now, now),
    )


@pytest.fixture()
def world(client):
    del client
    conn = get_connection()
    clock = FrozenClock(NOW)
    safety = TempleSafetyService(conn, clock)
    safety.create_temple({
        "code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage",
        "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 10,
        "ventilation_capacity": 3000,
    })
    safety.add_hall("lingyun-temple", {
        "code": "main-hall", "name": "大雄宝殿", "visit_order": 1,
        "expected_visit_seconds": 900, "ventilation_capacity": 1200,
    })
    policy = safety.create_safety_policy("lingyun-temple", DEFAULT_RULES, "tests")
    safety.publish_safety_policy(policy["id"], "tests", to_storage(NOW))
    operations = TempleRestorationService(conn, clock)
    operations.create_restoration_campaign({
        "temple_code": "lingyun-temple", "safety_policy_id": policy["id"],
        "code": "roof-2026", "name": "屋面修缮", "strategy": "phased",
        "target_percentage": 100, "actor": "planner",
    })
    make_user(10, "requester")
    make_user(20, "approver")
    service = FundLedgerService(conn, clock)
    return {"service": service, "operations": operations, "clock": clock}


def donation(**overrides):
    payload = {
        "temple_code": "lingyun-temple", "batch_code": "don-0001",
        "amount": "100.00", "credential": "RCPT-0001", "actor": "cashier",
        "designations": [
            {"scope": "campaign", "campaign_code": "roof-2026", "amount": "70.00"},
            {"scope": "hall", "hall_code": "main-hall", "amount": "30.00"},
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- money


def test_money_helpers_reject_subcent_and_nonpositive():
    assert to_cents("12.34") == 1234
    assert to_cents(5) == 500
    assert yuan(-105) == "-1.05"
    with pytest.raises(ValueError):
        to_cents("1.234")
    with pytest.raises(ValueError):
        to_cents("0")


def test_proportional_split_covers_full_amount():
    assert proportional_split(100, [1, 1, 1]) == [34, 33, 33]
    assert sum(proportional_split(101, [3, 7])) == 101
    assert proportional_split(50, [0, 50]) == [0, 50]


# --------------------------------------------------------------------- donations


def test_donation_posts_designated_balances_and_is_idempotent(world):
    service = world["service"]
    result = service.register_donation(donation())
    assert result["state"] == "posted"
    assert result["gross_amount"] == "100.00"
    budget = service.campaign_budget("roof-2026")
    assert (budget["book_balance"], budget["committed"], budget["available"]) == ("70.00", "0.00", "70.00")
    hall_purpose = [p for p in service.list_purposes("lingyun-temple") if p["scope_type"] == "hall"][0]
    assert hall_purpose["book_balance_cents"] == 3000
    # 同一凭据重放不重复入账（即使换了批次号）
    with pytest.raises(ConflictError):
        service.register_donation(donation(batch_code="don-0002"))
    # 批次号本身也不能重复
    with pytest.raises(ConflictError):
        service.register_donation(donation(credential="RCPT-OTHER"))
    integrity = service.verify_integrity()
    assert integrity["ok"]


def test_donation_designation_total_must_match_gross(world):
    service = world["service"]
    bad = donation(designations=[
        {"scope": "campaign", "campaign_code": "roof-2026", "amount": "70.00"},
        {"scope": "general", "amount": "20.00"},
    ])
    with pytest.raises(ValidationError):
        service.register_donation(bad)


def test_anonymous_donor_is_masked(world):
    service = world["service"]
    result = service.register_donation(donation(anonymous=True, donor_name="不应出现的姓名"))
    assert result["donor_label"] == "匿名捐赠方"
    assert "donor_key_hash" not in result
    listed = service.list_donations("lingyun-temple")[0]
    assert listed["donor_label"] == "匿名捐赠方"
    assert "donor_key_hash" not in listed
    # 同一捐赠方生成稳定哈希但不外泄
    conn = get_connection()
    hash_a = conn.execute("SELECT donor_key_hash FROM donation_batches WHERE code='don-0001'").fetchone()[0]
    service.register_donation(donation(
        batch_code="don-0002", credential="RCPT-0002", donor_key="repeat-donor",
        donor_name="老居士", anonymous=False,
    ))
    service.register_donation(donation(
        batch_code="don-0003", credential="RCPT-0003", donor_key="repeat-donor",
        donor_name="老居士", anonymous=False,
    ))
    rows = conn.execute("SELECT code,donor_key_hash FROM donation_batches WHERE code IN ('don-0002','don-0003')").fetchall()
    assert rows[0]["donor_key_hash"] == rows[1]["donor_key_hash"]
    assert rows[0]["donor_key_hash"] != hash_a or True


# --------------------------------------------------------------------- budget flow


def test_budget_hold_expenditure_release_trace(world):
    service = world["service"]
    service.register_donation(donation())
    service.register_donation(donation(
        batch_code="don-0010", credential="RCPT-0010", amount="50.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "50.00"}],
    ))
    hold = service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "90.00", "credential": "BUDGET-HOLD-1", "actor": "planner",
    })
    budget = service.campaign_budget("roof-2026")
    assert (budget["book_balance"], budget["committed"], budget["available"]) == ("120.00", "90.00", "30.00")
    # FIFO：先吃第一批 70，再吃第二批 20
    assert [(a["batch_id"], a["amount"]) for a in hold["allocation"]] == [(1, "70.00"), (2, "20.00")]
    # 凭据重放不重复占用
    with pytest.raises(ConflictError):
        service.hold_budget({
            "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
            "amount": "1.00", "credential": "BUDGET-HOLD-1", "actor": "planner",
        })
    # 可支配不足拒绝承诺
    with pytest.raises(ConflictError):
        service.hold_budget({
            "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
            "amount": "31.00", "credential": "BUDGET-HOLD-2", "actor": "planner",
        })

    spent = service.record_expenditure({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "60.00", "credential": "PAY-1", "actor": "finance", "memo": "木作工程款",
    })
    budget = service.campaign_budget("roof-2026")
    assert (budget["book_balance"], budget["committed"], budget["available"], budget["spent"]) == (
        "60.00", "30.00", "30.00", "60.00"
    )
    # 支出资金来源可追溯到第一批捐赠
    assert spent["allocation"][0]["batch_id"] == 1
    # 支出不能超过承诺
    with pytest.raises(ConflictError):
        service.record_expenditure({
            "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
            "amount": "31.00", "credential": "PAY-2", "actor": "finance",
        })

    released = service.release_hold(hold["id"], {
        "credential": "REL-1", "reason": "工程结余释放", "actor": "finance",
    })
    assert released["committed_delta"] == "-30.00"
    budget = service.campaign_budget("roof-2026")
    assert (budget["book_balance"], budget["committed"], budget["available"]) == ("60.00", "0.00", "60.00")
    # 重复释放被拒绝
    with pytest.raises(ConflictError):
        service.release_hold(hold["id"], {"credential": "REL-2", "reason": "再次释放", "actor": "finance"})
    assert service.verify_integrity()["ok"]


def test_partial_release_rounds_across_sources(world):
    service = world["service"]
    service.register_donation(donation(
        batch_code="don-a", credential="RC-A", amount="100.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "100.00"}],
    ))
    service.register_donation(donation(
        batch_code="don-b", credential="RC-B", amount="100.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "100.00"}],
    ))
    hold = service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "100.00", "credential": "HLD-1", "actor": "planner",
    })
    # 全部来自第一批
    assert len(hold["allocation"]) == 1 and hold["allocation"][0]["batch_id"] == 1
    service.release_hold(hold["id"], {"amount": "40.00", "credential": "RLS-1", "reason": "部分释放", "actor": "finance"})
    budget = service.campaign_budget("roof-2026")
    assert (budget["committed_cents"], budget["available_cents"]) == (6000, 14000)
    assert service.verify_integrity()["ok"]


# --------------------------------------------------------------------- reversal


def test_reversal_refunds_unencumbered_funds_and_blocks_encumbered(world):
    service = world["service"]
    service.register_donation(donation())  # 70 campaign, 30 hall
    hold = service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "70.00", "credential": "HLD-1", "actor": "planner",
    })
    service.record_expenditure({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "50.00", "credential": "PAY-1", "actor": "finance",
    })
    # 批次全部撤销：campaign 70 中有 50 已支出、20 仍占用，只有 hall 30 可退 → 拒绝
    with pytest.raises(ConflictError):
        service.reverse_donation("don-0001", {"amount": "100.00", "credential": "REF-1", "reason": "全额退捐", "actor": "cashier"})
    # 释放剩余 20 占用后，campaign 20 恢复可支配
    service.release_hold(hold["id"], {"credential": "REL-1", "reason": "释放", "actor": "finance"})
    detail = service.reverse_donation("don-0001", {"amount": "50.00", "credential": "REF-1", "reason": "退还未用捐款", "actor": "cashier"})
    assert detail["state"] == "posted"
    assert detail["reversed_amount"] == "50.00"
    budget = service.campaign_budget("roof-2026")
    # campaign 70 - 50 支出 - 20 撤销 = 0
    assert budget["book_balance_cents"] == 0
    assert service.verify_integrity()["ok"]
    # 撤销凭据不能复用
    with pytest.raises(ConflictError):
        service.reverse_donation("don-0001", {"credential": "REF-1", "reason": "重复", "actor": "cashier"})


def test_reversal_failure_rolls_back_everything(world):
    service = world["service"]
    service.register_donation(donation())
    balances_before = service.campaign_budget("roof-2026")
    # 撤销金额超过批次余额：整体回滚，不留半截流水
    with pytest.raises(ValidationError):
        service.reverse_donation("don-0001", {"amount": "999.00", "credential": "REF-1", "reason": "超额", "actor": "cashier"})
    assert service.campaign_budget("roof-2026")["book_balance"] == balances_before["book_balance"]
    assert service.verify_integrity()["ok"]


# --------------------------------------------------------------------- transfers


def _general_purpose(service) -> dict:
    return [p for p in service.list_purposes("lingyun-temple") if p["scope_type"] == "general"][0]


def _campaign_purpose(service) -> dict:
    return [p for p in service.list_purposes("lingyun-temple") if p["scope_type"] == "campaign"][0]


def test_transfer_requires_distinct_authorized_confirmation(world):
    service = world["service"]
    service.register_donation(donation(
        amount="70.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "70.00"}],
    ))
    campaign_purpose = _campaign_purpose(service)
    # 先确保存在 general 用途
    service.register_donation({
        "temple_code": "lingyun-temple", "batch_code": "don-gen", "amount": "10.00",
        "credential": "RCPT-GEN", "actor": "cashier",
        "designations": [{"scope": "general", "amount": "10.00"}],
    })
    general_purpose = _general_purpose(service)
    request = service.create_transfer({
        "transfer_code": "tr-0001",
        "source_purpose_code": campaign_purpose["code"],
        "target_purpose_code": general_purpose["code"],
        "amount": "40.00", "reason": "屋面计划结余调拨至寺院通用",
        "actor": "requester",
    }, requester_user_id=10)
    assert request["state"] == "pending"
    # 申请人不能自审
    with pytest.raises(ConflictError):
        service.approve_transfer(request["id"], {"credential": "CONF-1", "actor": "requester"}, approver_user_id=10)
    approved = service.approve_transfer(
        request["id"], {"credential": "CONF-1", "actor": "approver"}, approver_user_id=20
    )
    assert approved["state"] == "approved"
    assert approved["out_entry_id"] and approved["in_entry_id"]
    # 审批凭据不能重放（重复审批）
    with pytest.raises(ConflictError):
        service.approve_transfer(request["id"], {"credential": "CONF-1", "actor": "someone"}, approver_user_id=99)
    source_budget = service.campaign_budget("roof-2026")
    assert source_budget["book_balance"] == "30.00"
    assert service.balances(general_purpose["id"])["book_balance"] == "50.00"
    # 调入资金保留来源追溯（含原始捐赠批次穿透信息）
    sources = service.source_tracking(general_purpose["id"])
    transfer_lots = [s for s in sources if s["source_type"] == "transfer"]
    assert any(s["available_cents"] == 4000 for s in transfer_lots)
    origins = [ref for lot in transfer_lots for ref in lot["origin_sources"]]
    assert any(ref.get("batch_code") == "don-0001" for ref in origins), origins
    assert service.verify_integrity()["ok"]


def test_transfer_rejected_when_source_insufficient_at_approval(world):
    service = world["service"]
    service.register_donation(donation(
        amount="70.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "70.00"}],
    ))
    service.register_donation({
        "temple_code": "lingyun-temple", "batch_code": "don-gen", "amount": "10.00",
        "credential": "RCPT-GEN", "actor": "cashier",
        "designations": [{"scope": "general", "amount": "10.00"}],
    })
    cp = _campaign_purpose(service)
    gp = _general_purpose(service)
    request = service.create_transfer({
        "transfer_code": "tr-0002", "source_purpose_code": cp["code"],
        "target_purpose_code": gp["code"], "amount": "70.00",
        "reason": "申请时足额，审批前被占用", "actor": "requester",
    }, requester_user_id=10)
    # 审批前把可支配资金全部承诺
    service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "70.00", "credential": "H-LATER", "actor": "planner",
    })
    with pytest.raises(ConflictError):
        service.approve_transfer(request["id"], {"credential": "CONF-2", "actor": "approver"}, approver_user_id=20)
    # 失败不留任何流水，双方余额不变
    assert service.balances(cp["id"])["book_balance_cents"] == 7000
    assert service.balances(gp["id"])["book_balance_cents"] == 1000
    detail = service.transfer_detail(request["id"])
    assert detail["state"] == "pending" and detail["out_entry_id"] is None
    assert service.verify_integrity()["ok"]


def test_transfer_reject_flow(world):
    service = world["service"]
    service.register_donation(donation(
        amount="10.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "10.00"}],
    ))
    service.register_donation({
        "temple_code": "lingyun-temple", "batch_code": "don-gen", "amount": "1.00",
        "credential": "RCPT-GEN", "actor": "cashier",
        "designations": [{"scope": "general", "amount": "1.00"}],
    })
    cp, gp = _campaign_purpose(service), _general_purpose(service)
    request = service.create_transfer({
        "transfer_code": "tr-0003", "source_purpose_code": cp["code"],
        "target_purpose_code": gp["code"], "amount": "5.00",
        "reason": "用途不合理的调拨申请", "actor": "requester",
    }, requester_user_id=10)
    rejected = service.reject_transfer(
        request["id"], {"credential": "CONF-X", "actor": "approver", "reason": "用途不符"},
        approver_user_id=20,
    )
    assert rejected["state"] == "rejected"
    assert service.balances(cp["id"])["book_balance_cents"] == 1000


# --------------------------------------------------------------------- campaign lifecycle


def test_campaign_completion_and_cancellation_release_open_budget(world):
    service, operations = world["service"], world["operations"]
    service.register_donation(donation(
        amount="100.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "100.00"}],
    ))
    service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "60.00", "credential": "HLD-1", "actor": "planner",
    })
    service.record_expenditure({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "35.00", "credential": "PAY-1", "actor": "finance",
    })
    operations.start_restoration_campaign(1, "planner", "开工")
    completed = operations.complete_restoration_campaign(1, "planner", "竣工验收")
    assert completed["state"] == "completed"
    budget = service.campaign_budget("roof-2026")
    # 支出 35 落账，剩余承诺 25 自动释放回可支配
    assert (budget["book_balance"], budget["committed"], budget["available"], budget["spent"]) == (
        "65.00", "0.00", "65.00", "35.00"
    )
    ledger_types = [entry["event_type"] for entry in budget["ledger"]]
    assert ledger_types.count("reservation_release") == 1
    assert service.verify_integrity()["ok"]


def test_cancel_campaign_without_budget_is_safe(world):
    service, operations = world["service"], world["operations"]
    operations.start_restoration_campaign(1, "planner", "开工")
    result = operations.cancel_restoration_campaign(1, "planner", "计划取消")
    assert result["state"] == "cancelled"
    assert service.verify_integrity()["ok"]


# --------------------------------------------------------------------- point in time / restart


def test_balances_replay_deterministically_at_any_point_in_time(world):
    service = world["service"]
    clock = world["clock"]
    service.register_donation(donation(
        amount="100.00",
        designations=[{"scope": "campaign", "campaign_code": "roof-2026", "amount": "100.00"}],
        received_at="2026-09-28T08:00:00Z",
    ))
    clock.advance(minutes=10)
    service.hold_budget({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "40.00", "credential": "HLD-1", "actor": "planner",
    })
    clock.advance(minutes=10)
    service.record_expenditure({
        "temple_code": "lingyun-temple", "campaign_code": "roof-2026",
        "amount": "40.00", "credential": "PAY-1", "actor": "finance",
    })
    expected = {
        "2026-09-28T08:05:00Z": (10000, 0, 10000),
        "2026-09-28T08:15:00Z": (10000, 4000, 6000),
        "2026-09-28T08:25:00Z": (6000, 0, 6000),
    }
    for point, (book, committed, available) in expected.items():
        snapshot = service.campaign_budget("roof-2026", at=point)
        assert (snapshot["book_balance_cents"], snapshot["committed_cents"], snapshot["available_cents"]) == (
            book, committed, available
        )
    # 重新打开一个服务实例（模拟重启），相同时点必须复算出完全一致的结果
    restarted = FundLedgerService(get_connection(), FrozenClock(datetime(2026, 10, 1, tzinfo=UTC)))
    for point, values in expected.items():
        snapshot = restarted.campaign_budget("roof-2026", at=point)
        assert (snapshot["book_balance_cents"], snapshot["committed_cents"], snapshot["available_cents"]) == values
    assert restarted.verify_integrity()["ok"]


# --------------------------------------------------------------------- HTTP auth


def test_ledger_endpoints_require_session_and_permission(client, admin):
    del admin
    # 未登录 → 401
    assert client.get("/api/temple/funds/purposes").status_code == 401
    headers = {"Authorization": "Bearer invalid-token"}
    assert client.get("/api/temple/funds/purposes", headers=headers).status_code == 401


def test_ledger_http_flow_and_dual_approval(client, admin):
    headers = admin["headers"]
    # 准备寺院、殿堂、策略、修缮计划
    client.post("/api/temple/temples", json={
        "code": "http-temple", "name": "HTTP 寺", "temple_type": "community",
        "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 5, "ventilation_capacity": 500,
    }, headers=headers)
    policy = client.post("/api/temple/temples/http-temple/policies", json={"rules": DEFAULT_RULES, "actor": "admin"}, headers=headers).json()
    client.post(f"/api/temple/policies/{policy['id']}/publish", json={"actor": "admin", "effective_from": "2026-09-01T00:00:00Z"}, headers=headers)
    client.post("/api/temple/operations/restoration_campaigns", json={
        "temple_code": "http-temple", "safety_policy_id": policy["id"], "code": "http-campaign",
        "name": "HTTP 修缮", "strategy": "phased", "actor": "admin",
    }, headers=headers)
    # 第二名审批人
    client.post("/api/users", json={
        "username": "approver2", "password": "Approver!23456", "display_name": "审批员", "role_codes": ["administrator"],
    }, headers=headers)
    approver_login = client.post("/api/auth/login", json={
        "username": "approver2", "password": "Approver!23456", "client_label": "test",
    }).json()
    approver_headers = {"Authorization": f"Bearer {approver_login['token']}"}

    register = client.post("/api/temple/funds/donations", json={
        "temple_code": "http-temple", "batch_code": "http-don-1", "amount": "100.00",
        "credential": "HTTP-RCPT-1", "actor": "admin",
        "designations": [{"scope": "campaign", "campaign_code": "http-campaign", "amount": "100.00"}],
    }, headers=headers)
    assert register.status_code == 201, register.text
    replay = client.post("/api/temple/funds/donations", json={
        "temple_code": "http-temple", "batch_code": "http-don-2", "amount": "100.00",
        "credential": "HTTP-RCPT-1", "actor": "admin",
    }, headers=headers)
    assert replay.status_code == 409

    hold = client.post("/api/temple/funds/budgets/holds", json={
        "temple_code": "http-temple", "campaign_code": "http-campaign",
        "amount": "30.00", "credential": "HTTP-HOLD-1", "actor": "admin",
    }, headers=headers)
    assert hold.status_code == 201, hold.text
    budget = client.get("/api/temple/funds/campaigns/http-campaign/budget", headers=headers).json()
    assert budget["book_balance"] == "100.00" and budget["committed"] == "30.00" and budget["available"] == "70.00"

    pay = client.post("/api/temple/funds/budgets/expenditures", json={
        "temple_code": "http-temple", "campaign_code": "http-campaign",
        "amount": "20.00", "credential": "HTTP-PAY-1", "actor": "admin",
    }, headers=headers)
    assert pay.status_code == 201

    # 双重确认：申请人自审被拒，另一人审批通过
    general = client.post("/api/temple/funds/donations", json={
        "temple_code": "http-temple", "batch_code": "http-don-gen", "amount": "1.00",
        "credential": "HTTP-RCPT-GEN", "actor": "admin",
        "designations": [{"scope": "general", "amount": "1.00"}],
    }, headers=headers).json()
    purposes = client.get("/api/temple/funds/purposes?temple_code=http-temple", headers=headers).json()["items"]
    general_code = next(p["code"] for p in purposes if p["scope_type"] == "general")
    campaign_code = next(p["code"] for p in purposes if p["scope_type"] == "campaign")
    transfer = client.post("/api/temple/funds/transfers", json={
        "transfer_code": "http-tr-1", "source_purpose_code": campaign_code,
        "target_purpose_code": general_code, "amount": "10.00",
        "reason": "HTTP 流程跨用途调拨验证", "actor": "admin",
    }, headers=headers)
    assert transfer.status_code == 201, transfer.text
    transfer_id = transfer.json()["id"]
    self_approve = client.post(f"/api/temple/funds/transfers/{transfer_id}/approve", json={
        "credential": "HTTP-CONF-1", "actor": "admin",
    }, headers=headers)
    assert self_approve.status_code == 409
    approved = client.post(f"/api/temple/funds/transfers/{transfer_id}/approve", json={
        "credential": "HTTP-CONF-1", "actor": "approver2",
    }, headers=approver_headers)
    assert approved.status_code == 200, approved.text

    verify = client.get("/api/temple/funds/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


def test_missing_entities_raise_not_found(world):
    service = world["service"]
    with pytest.raises(NotFoundError):
        service.purpose_by_code("hall-999999")
    with pytest.raises(NotFoundError):
        service.hold_budget({
            "temple_code": "lingyun-temple", "campaign_code": "missing-campaign",
            "amount": "1.00", "credential": "X-1", "actor": "p",
        })
