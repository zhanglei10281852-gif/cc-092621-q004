from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.temple.funds import FundLedgerService, donor_digest, format_minor
from app.temple.operations import TempleRestorationService
from app.temple.rules import DEFAULT_RULES
from app.temple.service import TempleSafetyService

START = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPLE_DATABASE_PATH", str(tmp_path / "funds.db"))
    from app.database import close_connection

    close_connection()
    yield
    close_connection()


def build_world(clock: FrozenClock | None = None) -> tuple[TempleSafetyService, TempleRestorationService, FundLedgerService, FrozenClock]:
    clock = clock or FrozenClock(START)
    connection = get_connection()
    safety = TempleSafetyService(connection, clock)
    safety.create_temple({
        "code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage",
        "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 3000,
    })
    safety.add_hall("lingyun-temple", {"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200})
    policy = safety.create_safety_policy("lingyun-temple", DEFAULT_RULES, "tests")
    safety.publish_safety_policy(policy["id"], "tests", to_storage(clock.now()))
    operations = TempleRestorationService(connection, clock)
    operations.create_restoration_campaign({
        "temple_code": "lingyun-temple", "safety_policy_id": policy["id"], "code": "roof-2026",
        "name": "大雄宝殿屋顶修缮", "strategy": "phased", "target_percentage": 100,
        "hall_codes": [], "cohort_keys": [], "starts_at": None, "ends_at": None, "actor": "planner",
    })
    funds = FundLedgerService(connection, clock)
    funds.create_fund({"temple_code": "lingyun-temple", "code": "main-hall-roof", "name": "大雄宝殿屋面专项资金", "restriction_type": "hall", "hall_code": "main-hall", "actor": "finance"})
    funds.create_fund({"temple_code": "lingyun-temple", "code": "beam-component", "name": "梁架构件专项资金", "restriction_type": "component", "hall_code": "main-hall", "component_code": "beam", "actor": "finance"})
    return safety, operations, funds, clock


def donate(funds: FundLedgerService, fund_code: str, amount: str, *, batch: str, receipt: str, donor: str = "张居士", received_at: str | None = None) -> dict:
    return funds.record_donation({
        "fund_code": fund_code, "batch_code": batch, "receipt_no": receipt, "donor_ref": donor,
        "amount": amount, "received_at": received_at or to_storage(funds.clock.now()),
        "channel": "bank", "note": "", "actor": "finance",
    })


def balances(funds: FundLedgerService, fund_code: str = "main-hall-roof", **kwargs) -> dict:
    return funds.fund_detail(fund_code, **kwargs)


# --------------------------------------------------------------------------- 入账与幂等

def test_donation_records_anonymized_donor_and_balances():
    _, _, funds, _ = build_world()
    result = donate(funds, "main-hall-roof", "150.00", batch="b-001", receipt="R-001", donor="SecretDonor-XYZ")
    assert result["replayed"] is False
    assert result["donor_hash"] == donor_digest("SecretDonor-XYZ")
    assert len(result["donor_hash"]) == 64
    detail = balances(funds)
    assert detail["book_balance_minor"] == 15000
    assert detail["committed_minor"] == 0
    assert detail["available_minor"] == 15000
    assert detail["book_balance"] == "150.00"
    # 明文捐赠方标识不落库
    raw = get_connection().execute("SELECT COUNT(*) FROM donation_batches WHERE donor_hash LIKE '%SecretDonor%'").fetchone()[0]
    assert raw == 0


def test_same_receipt_replays_donation_and_conflicting_payload_rejected():
    _, _, funds, _ = build_world()
    first = donate(funds, "main-hall-roof", "150.00", batch="b-001", receipt="R-001")
    replay = donate(funds, "main-hall-roof", "150.00", batch="b-001", receipt="R-001")
    assert replay["replayed"] is True
    assert replay["id"] == first["id"]
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM fund_ledger_entries WHERE entry_type='donation'").fetchone()[0] == 1
    with pytest.raises(Exception) as exc:
        donate(funds, "main-hall-roof", "200.00", batch="b-001", receipt="R-001")
    assert exc.value.code == "conflict"
    assert connection.execute("SELECT COUNT(*) FROM fund_ledger_entries WHERE entry_type='donation'").fetchone()[0] == 1


def test_command_key_replay_cannot_double_post_reversal():
    _, _, funds, _ = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001")
    payload = {"receipt_no": "R-001", "amount": "40.00", "command_key": "cmd-reverse-001", "reason": "凭据作废退款", "actor": "finance"}
    first = funds.reverse_donation(payload)
    assert first["replayed"] is False
    replay = funds.reverse_donation(payload)
    assert replay["replayed"] is True
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM fund_ledger_entries WHERE entry_type='reversal'").fetchone()[0] == 1
    assert balances(funds)["book_balance_minor"] == 6000
    # 撤销超过未动用部分必须拒绝
    with pytest.raises(Exception) as exc:
        funds.reverse_donation({**payload, "amount": "70.00", "command_key": "cmd-reverse-002"})
    assert exc.value.code == "conflict"
    assert balances(funds)["book_balance_minor"] == 6000


# --------------------------------------------------------------------------- 承诺/支出/释放

def test_commitment_expenditure_release_lifecycle_and_overspend_failure():
    _, _, funds, _ = build_world()
    donate(funds, "main-hall-roof", "150.00", batch="b-001", receipt="R-001")
    commit = funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-001", "amount": "100.00", "reason": "瓦作采购", "actor": "planner"})
    assert commit["replayed"] is False
    detail = balances(funds)
    assert detail["book_balance_minor"] == 15000
    assert detail["committed_minor"] == 10000
    assert detail["available_minor"] == 5000
    # 超可支配承诺被拒绝，无任何残留流水
    with pytest.raises(Exception) as exc:
        funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-002", "amount": "60.00", "reason": "超额", "actor": "planner"})
    assert exc.value.code == "conflict"
    spent = funds.record_expenditure("c-001", {"amount": "60.00", "command_key": "cmd-pay-001", "reason": "首期瓦款", "actor": "finance"})
    assert spent["spent_minor"] == 6000
    assert spent["state"] == "partially_settled"
    assert balances(funds)["committed_minor"] == 4000
    # 超过承诺占用的支出失败且不留痕
    entries_before = get_connection().execute("SELECT COUNT(*) FROM fund_ledger_entries").fetchone()[0]
    with pytest.raises(Exception) as exc:
        funds.record_expenditure("c-001", {"amount": "50.00", "command_key": "cmd-pay-bad", "reason": "超付", "actor": "finance"})
    assert exc.value.code == "conflict"
    assert get_connection().execute("SELECT COUNT(*) FROM fund_ledger_entries").fetchone()[0] == entries_before
    # 同一支出凭据重放不重复入账
    replay = funds.record_expenditure("c-001", {"amount": "60.00", "command_key": "cmd-pay-001", "reason": "首期瓦款", "actor": "finance"})
    assert replay["replayed"] is True
    funds.release_commitment("c-001", {"amount": None, "command_key": "cmd-release-001", "reason": "尾款取消", "actor": "finance"})
    detail = balances(funds)
    assert detail["committed_minor"] == 0
    assert detail["available_minor"] == 9000
    assert detail["book_balance_minor"] == 9000
    assert funds.verify_integrity()["consistent"] is True


def test_campaign_cancel_releases_outstanding_commitments_atomically():
    _, operations, funds, _ = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001")
    funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-001", "amount": "60.00", "reason": "瓦作", "actor": "planner"})
    funds.record_expenditure("c-001", {"amount": "20.00", "command_key": "cmd-pay-001", "reason": "定金", "actor": "finance"})
    campaign_id = get_connection().execute("SELECT id FROM restoration_campaigns WHERE code='roof-2026'").fetchone()[0]
    cancelled = operations.cancel_restoration_campaign(campaign_id, "director", "规划取消")
    assert cancelled["state"] == "cancelled"
    detail = balances(funds)
    assert detail["committed_minor"] == 0
    assert detail["book_balance_minor"] == 8000
    assert detail["available_minor"] == 8000
    commitment = get_connection().execute("SELECT state,spent_minor,released_minor FROM budget_commitments WHERE code='c-001'").fetchone()
    assert commitment["state"] == "settled"
    assert commitment["released_minor"] == 4000
    # 已取消项目不能再承诺
    with pytest.raises(Exception) as exc:
        funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-002", "amount": "10.00", "reason": "x", "actor": "planner"})
    assert exc.value.code == "conflict"
    assert funds.verify_integrity()["consistent"] is True


# --------------------------------------------------------------------------- 资金来源追踪（FIFO）

def test_expenditure_traces_back_to_donation_batches_fifo():
    _, _, funds, clock = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001", received_at=to_storage(clock.now()))
    clock.advance(seconds=10)
    donate(funds, "main-hall-roof", "50.00", batch="b-002", receipt="R-002", received_at=to_storage(clock.now()))
    funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-001", "amount": "120.00", "reason": "瓦作", "actor": "planner"})
    funds.record_expenditure("c-001", {"amount": "120.00", "command_key": "cmd-pay-001", "reason": "工程款", "actor": "finance"})
    connection = get_connection()
    allocations = connection.execute(
        "SELECT db.batch_code,a.amount_minor FROM fund_source_allocations a "
        "JOIN fund_ledger_entries e ON e.id=a.ledger_entry_id "
        "LEFT JOIN donation_batches db ON db.id=a.donation_batch_id "
        "WHERE e.entry_type='expenditure' ORDER BY a.id"
    ).fetchall()
    assert [(row[0], row[1]) for row in allocations] == [("b-001", 10000), ("b-002", 2000)]
    sources = funds.fund_sources("main-hall-roof")["items"]
    assert len(sources) == 1
    assert sources[0]["batch_code"] == "b-002"
    assert sources[0]["remaining_minor"] == 3000
    assert funds.verify_integrity()["consistent"] is True


# --------------------------------------------------------------------------- 跨用途调拨双重确认

def test_transfer_requires_distinct_authorized_approver_and_moves_sources():
    _, _, funds, _ = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001")
    with pytest.raises(Exception) as exc:
        funds.propose_transfer({"code": "t-001", "command_key": "cmd-prop-001", "from_fund_code": "main-hall-roof", "to_fund_code": "beam-component", "amount": "30.00", "reason": "调整用途支援梁架", "proposed_by": "alice", "required_approver": "alice"})
    assert exc.value.code == "conflict"
    order = funds.propose_transfer({"code": "t-001", "command_key": "cmd-prop-001", "from_fund_code": "main-hall-roof", "to_fund_code": "beam-component", "amount": "30.00", "reason": "调整用途支援梁架", "proposed_by": "alice", "required_approver": "bob"})
    assert order["state"] == "proposed"
    # 非指定批准人不能确认
    with pytest.raises(Exception) as exc:
        funds.confirm_transfer("t-001", {"approver": "carol", "command_key": "cmd-confirm-001"})
    assert exc.value.code == "transfer_approval_invalid"
    # 指定批准人（大小写不敏感）确认
    confirmed = funds.confirm_transfer("t-001", {"approver": "BOB", "command_key": "cmd-confirm-001"})
    assert confirmed["state"] == "confirmed"
    assert len(confirmed["entries"]) == 2
    assert balances(funds, "main-hall-roof")["book_balance_minor"] == 7000
    assert balances(funds, "beam-component")["book_balance_minor"] == 3000
    # 重放确认不会再次入账
    replay = funds.confirm_transfer("t-001", {"approver": "BOB", "command_key": "cmd-confirm-001"})
    assert replay["replayed"] is True
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM fund_ledger_entries WHERE transfer_group_code='transfer:t-001'").fetchone()[0] == 2
    # 调入资金成为新来源桶，可继续追溯
    sources = funds.fund_sources("beam-component")["items"]
    assert sources[0]["kind"] == "transfer_in"
    assert sources[0]["counterparty_fund_code"] == "main-hall-roof"
    assert sources[0]["remaining_minor"] == 3000
    # 再次确认已完成调拨必须失败
    with pytest.raises(Exception) as exc:
        funds.confirm_transfer("t-001", {"approver": "BOB", "command_key": "cmd-confirm-002"})
    assert exc.value.code == "conflict"
    assert funds.verify_integrity()["consistent"] is True


def test_transfer_confirm_failure_rolls_back_everything(monkeypatch):
    _, _, funds, _ = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001")
    funds.propose_transfer({"code": "t-002", "command_key": "cmd-prop-002", "from_fund_code": "main-hall-roof", "to_fund_code": "beam-component", "amount": "30.00", "reason": "x", "proposed_by": "alice", "required_approver": "bob"})
    calls = {"count": 0}
    original = FundLedgerService._insert_entry

    def flaky(self, connection, **values):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash on second leg")
        return original(self, connection, **values)

    monkeypatch.setattr(FundLedgerService, "_insert_entry", flaky)
    with pytest.raises(RuntimeError):
        funds.confirm_transfer("t-002", {"approver": "bob", "command_key": "cmd-confirm-002"})
    connection = get_connection()
    assert connection.execute("SELECT COUNT(*) FROM fund_ledger_entries WHERE transfer_group_code='transfer:t-002'").fetchone()[0] == 0
    assert connection.execute("SELECT state FROM fund_transfer_orders WHERE code='t-002'").fetchone()["state"] == "proposed"
    assert balances(funds, "main-hall-roof")["book_balance_minor"] == 10000
    # 凭据未被占用，恢复后可以用同一凭据完成确认
    monkeypatch.undo()
    confirmed = funds.confirm_transfer("t-002", {"approver": "bob", "command_key": "cmd-confirm-002"})
    assert confirmed["state"] == "confirmed"
    assert balances(funds, "beam-component")["book_balance_minor"] == 3000
    assert funds.verify_integrity()["consistent"] is True


def test_transfer_reject_releases_proposal():
    _, _, funds, _ = build_world()
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001")
    funds.propose_transfer({"code": "t-003", "command_key": "cmd-prop-003", "from_fund_code": "main-hall-roof", "to_fund_code": "beam-component", "amount": "30.00", "reason": "x", "proposed_by": "alice", "required_approver": "bob"})
    rejected = funds.reject_transfer("t-003", {"approver": "bob", "reason": "用途不合规"})
    assert rejected["state"] == "rejected"
    assert balances(funds, "main-hall-roof")["book_balance_minor"] == 10000
    with pytest.raises(Exception) as exc:
        funds.confirm_transfer("t-003", {"approver": "bob", "command_key": "cmd-confirm-003"})
    assert exc.value.code == "conflict"


# --------------------------------------------------------------------------- 时点复算与重启一致性

def test_balances_recompute_identically_at_any_point_after_restart():
    _, _, funds, clock = build_world()
    t0 = to_storage(clock.now())
    donate(funds, "main-hall-roof", "100.00", batch="b-001", receipt="R-001", received_at=t0)
    clock.advance(seconds=10)
    t1 = to_storage(clock.now())
    funds.commit_budget({"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-001", "amount": "60.00", "reason": "瓦作", "actor": "planner"})
    clock.advance(seconds=10)
    t2 = to_storage(clock.now())
    funds.record_expenditure("c-001", {"amount": "60.00", "command_key": "cmd-pay-001", "reason": "工程款", "actor": "finance"})
    clock.advance(seconds=10)
    t3 = to_storage(clock.now())
    donate(funds, "main-hall-roof", "50.00", batch="b-002", receipt="R-002", received_at=t3)

    def snapshot(service: FundLedgerService) -> dict:
        return {
            point: {
                key: service.fund_detail("main-hall-roof", as_of=point)[key]
                for key in ("book_balance_minor", "committed_minor", "available_minor")
            }
            for point in (t0, t1, t2, t3)
        }

    expected = {
        t0: {"book_balance_minor": 10000, "committed_minor": 0, "available_minor": 10000},
        t1: {"book_balance_minor": 10000, "committed_minor": 6000, "available_minor": 4000},
        t2: {"book_balance_minor": 4000, "committed_minor": 0, "available_minor": 4000},
        t3: {"book_balance_minor": 9000, "committed_minor": 0, "available_minor": 9000},
    }
    assert snapshot(funds) == expected
    # 模拟服务重启：换一个时钟与服务实例，时点复算结果必须一致
    restarted = FundLedgerService(get_connection(), FrozenClock(START + timedelta(days=2)))
    assert snapshot(restarted) == expected
    assert restarted.verify_integrity(as_of=t1)["consistent"] is True
    assert restarted.verify_integrity(as_of=t2)["consistent"] is True


# --------------------------------------------------------------------------- HTTP 冒烟

def test_funds_http_flow(client):
    client.post("/api/temple/temples", json={"code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 3000})
    client.post("/api/temple/temples/lingyun-temple/halls", json={"code": "main-hall", "name": "大雄宝殿", "visit_order": 1, "expected_visit_seconds": 900, "ventilation_capacity": 1200})
    policy = client.post("/api/temple/temples/lingyun-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-28T00:00:00Z"})
    client.post("/api/temple/operations/restoration_campaigns", json={"temple_code": "lingyun-temple", "safety_policy_id": policy["id"], "code": "roof-2026", "name": "屋顶修缮", "strategy": "phased", "target_percentage": 100, "actor": "planner"})
    created = client.post("/api/temple/funds", json={"temple_code": "lingyun-temple", "code": "main-hall-roof", "name": "屋面专项资金", "restriction_type": "hall", "hall_code": "main-hall", "actor": "finance"})
    assert created.status_code == 201, created.text
    donation = client.post("/api/temple/funds/donations", json={"fund_code": "main-hall-roof", "batch_code": "b-001", "receipt_no": "R-001", "donor_ref": "李居士", "amount": "150.00", "received_at": "2026-09-28T03:00:00Z", "actor": "finance"})
    assert donation.status_code == 201, donation.text
    detail = client.get("/api/temple/funds/main-hall-roof").json()
    assert detail["available"] == "150.00"
    commit = client.post("/api/temple/funds/commitments", json={"campaign_code": "roof-2026", "fund_code": "main-hall-roof", "code": "c-001", "amount": "100.00", "reason": "瓦作", "actor": "planner"})
    assert commit.status_code == 201, commit.text
    pay = client.post("/api/temple/funds/commitments/c-001/expenditures", json={"amount": "100.00", "command_key": "cmd-pay-http-1", "reason": "结清", "actor": "finance"})
    assert pay.status_code == 200, pay.text
    campaign = client.get("/api/temple/funds/campaigns/roof-2026").json()
    assert campaign["spent_minor"] == 10000
    assert campaign["held_minor"] == 0
    integrity = client.get("/api/temple/funds/integrity/verify").json()
    assert integrity["consistent"] is True


def test_amount_precision_and_negative_rejected(client):
    client.post("/api/temple/temples", json={"code": "lingyun-temple", "name": "凌云古寺", "temple_type": "heritage", "timezone": "Asia/Shanghai", "max_concurrent_mitigation_sessions": 10, "ventilation_capacity": 3000})
    client.post("/api/temple/funds", json={"temple_code": "lingyun-temple", "code": "main-hall-roof", "name": "屋面专项资金", "restriction_type": "unrestricted", "actor": "finance"})
    ok = client.post("/api/temple/funds/donations", json={"fund_code": "main-hall-roof", "batch_code": "b-001", "receipt_no": "R-001", "donor_ref": "李居士", "amount": "150.005", "received_at": "2026-09-28T03:00:00Z", "actor": "finance"})
    assert ok.status_code == 422
    bad = client.post("/api/temple/funds/donations", json={"fund_code": "main-hall-roof", "batch_code": "b-002", "receipt_no": "R-002", "donor_ref": "李居士", "amount": "0", "received_at": "2026-09-28T03:00:00Z", "actor": "finance"})
    assert bad.status_code == 422


def test_format_minor_helper():
    assert format_minor(0) == "0.00"
    assert format_minor(5) == "0.05"
    assert format_minor(12345) == "123.45"
    assert format_minor(-12345) == "-123.45"
