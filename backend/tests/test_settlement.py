"""检测结算共用口径的回归测试。

覆盖三类重点情形：应收金额为空、已收超应收、重复标记争议；同时守住重构前提——
结算单号、金额取值与接口结构不变，历史结算记录不被改写，三个入口与导出共用同一
套金额口径。
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.settlement import (
    MODULE,
    RECEIPT_EMPTY,
    RECEIPT_NONE,
    RECEIPT_OVER,
    RECEIPT_PARTIAL,
    RECEIPT_SETTLED,
    SettlementService,
    amount_snapshot,
    parse_amount,
)
from app.store import store


@pytest.fixture(autouse=True)
def restore_settlement_rows():
    """每个用例跑完都把结算表还原，用例之间互不影响。"""
    table = store.rows(MODULE)
    backup = copy.deepcopy(table)
    yield
    table[:] = backup


@pytest.fixture
def service() -> SettlementService:
    return SettlementService()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def add_row(**overrides: Any) -> dict[str, Any]:
    """往结算表直接塞一条测试记录，默认金额收讫、已开票、状态核对中。"""
    table = store.rows(MODULE)
    row: dict[str, Any] = {
        "id": max((int(r.get("id", 0)) for r in table), default=0) + 1,
        "status": "核对中",
        "pending": True,
        "abnormal": False,
        "结算单号": f"SETT-T{len(table) + 1:03d}",
        "委托单位": "测试单位",
        "结算周期": "2026-09",
        "应收金额": 100,
        "已收金额": 100,
        "开票状态": "已开票",
        "结算状态": "核对中",
    }
    row.update(overrides)
    table.append(row)
    return row


class TestParseAmount:
    def test_blank_values_are_none(self):
        assert parse_amount(None) is None
        assert parse_amount("") is None
        assert parse_amount("   ") is None
        assert parse_amount("abc") is None

    def test_numeric_values_quantize_to_cent(self):
        assert parse_amount(12.5) == Decimal("12.50")
        assert parse_amount("12.5") == Decimal("12.50")
        assert parse_amount(0) == Decimal("0.00")
        assert parse_amount("0.1") + parse_amount("0.2") == Decimal("0.30")


class TestAmountSnapshot:
    def test_blank_receivable(self):
        """应收金额为空：状态为金额待补录，未收金额给 None 而不是硬算。"""
        snap = amount_snapshot({"应收金额": None, "已收金额": 10})
        assert snap["收款状态"] == RECEIPT_EMPTY
        assert snap["未收金额"] is None

    def test_over_received(self):
        """已收超应收：状态为超额收款，未收金额为负。"""
        snap = amount_snapshot({"应收金额": 100, "已收金额": 120})
        assert snap["收款状态"] == RECEIPT_OVER
        assert snap["未收金额"] == -20.0

    def test_partial_and_settled(self):
        assert amount_snapshot({"应收金额": 100, "已收金额": 40})["收款状态"] == RECEIPT_PARTIAL
        snap = amount_snapshot({"应收金额": 100, "已收金额": 100})
        assert snap["收款状态"] == RECEIPT_SETTLED
        assert snap["未收金额"] == 0.0

    def test_blank_received_counts_as_unpaid(self):
        assert amount_snapshot({"应收金额": 100, "已收金额": None})["收款状态"] == RECEIPT_NONE


class TestRunAction:
    def test_start_check_blocked_when_receivable_blank(self, service):
        row = add_row(应收金额=None, status="待核对")
        entry, message = service.run_action(row["id"], "发起核对")
        assert entry is None
        assert "应收金额" in message
        assert row["status"] == "待核对"  # 被拦下时状态不被改写

    def test_confirm_blocked_when_receivable_blank(self, service):
        row = add_row(应收金额=None)
        entry, message = service.run_action(row["id"], "确认结算")
        assert entry is None
        assert "应收金额" in message

    def test_confirm_blocked_when_over_received(self, service):
        """已收超应收：确认结算被拦，状态保持不动。"""
        row = add_row(应收金额=100, 已收金额=120)
        entry, message = service.run_action(row["id"], "确认结算")
        assert entry is None
        assert "超过" in message
        assert row["status"] == "核对中"

    def test_confirm_blocked_when_under_received(self, service):
        row = add_row(应收金额=100, 已收金额=60)
        entry, message = service.run_action(row["id"], "确认结算")
        assert entry is None
        assert "收齐" in message

    def test_confirm_ok_when_settled_and_invoiced(self, service):
        row = add_row()
        entry, message = service.run_action(row["id"], "确认结算")
        assert entry is not None
        assert message == "结算单已确认结算"
        assert entry["status"] == "已收款"
        assert entry["收款状态"] == RECEIPT_SETTLED

    def test_confirm_blocked_by_invoice_pairing(self, service):
        row = add_row(开票状态="未开票")
        entry, message = service.run_action(row["id"], "确认结算")
        assert entry is None
        assert "开票" in message
        assert row["status"] == "核对中"

    def test_unrecognized_invoice_status_not_judged(self, service):
        """历史遗留的无法识别开票状态不妄判，保持既有行为。"""
        row = add_row(开票状态="检测结算样例X")
        entry, _ = service.run_action(row["id"], "确认结算")
        assert entry is not None

    def test_duplicate_dispute_blocked(self, service):
        """重复标记争议：第二次被拦下并说明原因，状态保持有争议。"""
        row = add_row()
        entry, _ = service.run_action(row["id"], "标记争议")
        assert entry is not None
        assert entry["status"] == "有争议"
        entry, message = service.run_action(row["id"], "标记争议")
        assert entry is None
        assert "重复" in message
        assert row["status"] == "有争议"

    def test_dispute_blocked_after_paid(self, service):
        row = add_row(status="已收款")
        entry, message = service.run_action(row["id"], "标记争议")
        assert entry is None
        assert "已收款" in message

    def test_dispute_not_blocked_by_amount_gap(self, service):
        """金额对不上正是标记争议的理由，争议不被金额口径拦。"""
        row = add_row(应收金额=None, 已收金额=999)
        entry, _ = service.run_action(row["id"], "标记争议")
        assert entry is not None

    def test_existing_messages_kept(self, service):
        entry, message = service.run_action(9999, "确认结算")
        assert entry is None
        assert message == "结算单 9999 不存在或已归档"
        row = add_row()
        entry, message = service.run_action(row["id"], "删除结算单")
        assert entry is None
        assert message == "动作「删除结算单」不属于检测结算可执行范围"


class TestApi:
    def test_export_not_shadowed_and_shares_amount_view(self, client):
        """导出不再被 /{entry_id} 影子路由拦截，且与列表共用同一套金额口径。"""
        response = client.get("/api/settlement/export")
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"module", "total", "items"}
        listed = client.get("/api/settlement", params={"size": 200}).json()["items"]
        by_number = {item["结算单号"]: item for item in listed}
        assert len(payload["items"]) == len(by_number)
        for item in payload["items"]:
            twin = by_number[item["结算单号"]]
            assert item["收款状态"] == twin["收款状态"]
            assert item["未收金额"] == twin["未收金额"]
            assert item["开票核对"] == twin["开票核对"]

    def test_detail_matches_list_view(self, client):
        listed = client.get("/api/settlement").json()["items"][0]
        detail = client.get(f"/api/settlement/{listed['id']}").json()
        assert detail["收款状态"] == listed["收款状态"]
        assert detail["未收金额"] == listed["未收金额"]
        assert detail["开票核对"] == listed["开票核对"]

    def test_list_structure_and_amounts_unchanged(self, client):
        """接口结构不变，结算单号与金额取值保持原样。"""
        payload = client.get("/api/settlement").json()
        assert set(payload) == {"items", "total", "page", "size"}
        first = payload["items"][0]
        assert first["结算单号"] == "SETT-0001"
        assert first["应收金额"] == 12.5
        assert first["已收金额"] == 12.5
        assert first["开票状态"] == "检测结算样例1"
        assert first["结算状态"] == "检测结算样例1"

    def test_action_result_structure(self, client):
        response = client.post("/api/settlement/1/actions", json={"values": {"action": "发起核对"}})
        payload = response.json()
        assert set(payload) == {"ok", "message", "entry"}
        assert payload["ok"] is True
        assert payload["message"] == "结算单已发起核对"
        assert payload["entry"]["status"] == "核对中"

    def test_action_rejections_are_readable(self, client):
        row = add_row(应收金额=100, 已收金额=120)
        payload = client.post(
            f"/api/settlement/{row['id']}/actions", json={"values": {"action": "确认结算"}}
        ).json()
        assert payload["ok"] is False
        assert "超过" in payload["message"]
        assert payload["entry"] is None

    def test_stored_rows_not_rewritten(self, client):
        """历史结算记录不能被改写：单号与金额原样保留，派生字段不落库。"""
        before = copy.deepcopy(store.rows(MODULE))
        client.get("/api/settlement/export")
        client.post("/api/settlement/1/actions", json={"values": {"action": "发起核对"}})
        after = store.rows(MODULE)
        assert len(before) == len(after)
        for old, new in zip(before, after):
            assert old["结算单号"] == new["结算单号"]
            assert old["应收金额"] == new["应收金额"]
            assert old["已收金额"] == new["已收金额"]
            assert "未收金额" not in new
            assert "收款状态" not in new
            assert "开票核对" not in new
