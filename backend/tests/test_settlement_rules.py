"""检测结算共用判定与服务行为测试。

运行：python3 -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import copy
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import settlement_rules as rules
from app.services.settlement import SettlementService
from app.store import store

MODULE = "settlement"


def make_entry(**overrides):
    entry = {
        "id": 999,
        "status": "待核对",
        "pending": True,
        "abnormal": False,
        "结算单号": "SETT-TEST",
        "委托单位": "测试单位",
        "结算周期": "2026-09",
        "检测项数": 1,
        "应收金额": Decimal("100.00"),
        "已收金额": Decimal("0.00"),
        "开票状态": rules.INVOICE_NOT_ISSUED,
        "结算状态": "未结算",
    }
    entry.update(overrides)
    return entry


class AmountViewTests(unittest.TestCase):
    def test_empty_and_blank_and_non_numeric_are_missing(self):
        for blank in (None, "", "   ", "待定"):
            view = rules.amount_view({"应收金额": blank, "已收金额": "50"})
            self.assertIsNone(view.receivable)
            self.assertFalse(view.receivable_missing is False)
            self.assertFalse(view.overpaid)
            self.assertFalse(view.paid_in_full)

    def test_numeric_strings_compare_by_decimal(self):
        view = rules.amount_view({"应收金额": "100.00", "已收金额": 100})
        self.assertTrue(view.paid_in_full)
        self.assertFalse(view.overpaid)

    def test_float_json_value_matches_decimal(self):
        # JSON 里的 0.3 解析成 float 后，按统一口径必须能与字符串 "0.3" 对上
        view = rules.amount_view({"应收金额": "0.3", "已收金额": 0.3})
        self.assertTrue(view.paid_in_full)

    def test_received_exceeds_receivable(self):
        view = rules.amount_view({"应收金额": "100", "已收金额": "120"})
        self.assertTrue(view.overpaid)
        self.assertFalse(view.paid_in_full)

    def test_missing_received_counts_as_zero(self):
        view = rules.amount_view({"应收金额": "100", "已收金额": None})
        self.assertFalse(view.paid_in_full)
        self.assertFalse(view.overpaid)

    def test_missing_receivable_never_overpaid(self):
        view = rules.amount_view({"应收金额": None, "已收金额": "999"})
        self.assertFalse(view.overpaid)
        self.assertFalse(view.paid_in_full)


class StartCheckTests(unittest.TestCase):
    def test_receivable_empty_blocks_start(self):
        for blank in (None, "", "  ", "未定价"):
            violation = rules.check_action("发起核对", make_entry(应收金额=blank))
            self.assertIsNotNone(violation)
            self.assertEqual(violation.code, "receivable_missing")

    def test_valid_start_passes(self):
        self.assertIsNone(rules.check_action("发起核对", make_entry()))


class ConfirmTests(unittest.TestCase):
    def _paid(self, **kw):
        params = {"已收金额": Decimal("100.00"), "开票状态": rules.INVOICE_ISSUED}
        params.update(kw)
        return make_entry(**params)

    def test_receivable_empty_blocks_confirm(self):
        violation = rules.check_action("确认结算", self._paid(应收金额=""))
        self.assertEqual(violation.code, "receivable_missing")

    def test_overpayment_blocks_confirm(self):
        violation = rules.check_action("确认结算", self._paid(已收金额="100.01"))
        self.assertEqual(violation.code, "received_exceeds_receivable")

    def test_underpayment_blocks_confirm(self):
        # 未登记开票状态（历史占位值）时走纯金额口径
        violation = rules.check_action(
            "确认结算", self._paid(已收金额="99.99", 开票状态="历史占位值")
        )
        self.assertEqual(violation.code, "received_not_in_full")

    def test_paid_but_not_invoiced_blocks_confirm(self):
        violation = rules.check_action(
            "确认结算", self._paid(开票状态=rules.INVOICE_NOT_ISSUED)
        )
        self.assertEqual(violation.code, "paid_without_invoice")

    def test_invoiced_but_not_paid_blocks_confirm(self):
        violation = rules.check_action(
            "确认结算",
            make_entry(应收金额="100", 已收金额="0", 开票状态=rules.INVOICE_ISSUED),
        )
        self.assertEqual(violation.code, "invoiced_without_payment")

    def test_paid_and_invoiced_passes(self):
        self.assertIsNone(rules.check_action("确认结算", self._paid()))

    def test_unknown_invoice_status_does_not_block_paid_order(self):
        # 历史数据里的占位开票状态不应把已收齐的结算单卡死
        entry = self._paid(开票状态="历史占位值")
        self.assertIsNone(rules.check_action("确认结算", entry))


class DisputeTests(unittest.TestCase):
    def test_duplicate_dispute_blocked(self):
        violation = rules.check_action("标记争议", make_entry(status="有争议"))
        self.assertEqual(violation.code, "duplicate_dispute")

    def test_first_dispute_passes_from_any_other_status(self):
        for status in ("待核对", "核对中", "已确认", "已收款"):
            self.assertIsNone(
                rules.check_action("标记争议", make_entry(status=status)), status
            )


class ServiceFlowTests(unittest.TestCase):
    def setUp(self):
        # 用一份隔离的副本，避免污染内存仓库里的种子历史记录
        self.original = copy.deepcopy(store.rows(MODULE))

    def tearDown(self):
        table = store.rows(MODULE)
        table[:] = self.original

    def _append(self, **overrides):
        rows = store.rows(MODULE)
        entry = make_entry(id=max(int(r["id"]) for r in rows) + 1, **overrides)
        rows.append(entry)
        return entry

    def test_start_flow_blocked_then_allowed(self):
        blocked = self._append(应收金额=None)
        result, message = SettlementService().run_action(blocked["id"], "发起核对")
        self.assertIsNone(result)
        self.assertIn("应收金额未登记", message)
        self.assertEqual(blocked["status"], "待核对")

        allowed = self._append(应收金额="200")
        result, message = SettlementService().run_action(allowed["id"], "发起核对")
        self.assertEqual(result["status"], "核对中")
        self.assertEqual(message, "结算单已发起核对")

    def test_confirm_rejects_overpayment_without_mutation(self):
        entry = self._append(应收金额="100", 已收金额="150", 开票状态="已开票")
        snapshot = copy.deepcopy(entry)
        result, message = SettlementService().run_action(entry["id"], "确认结算")
        self.assertIsNone(result)
        self.assertIn("超过应收金额", message)
        # 被拦下时历史记录的金额、状态、开票状态一律不动
        self.assertEqual(entry, snapshot)

    def test_duplicate_dispute_rejected(self):
        entry = self._append(status="有争议")
        result, message = SettlementService().run_action(entry["id"], "标记争议")
        self.assertIsNone(result)
        self.assertIn("重复标记争议", message)

    def test_happy_path_statuses_and_flags(self):
        service = SettlementService()
        entry = self._append(已收金额="100", 开票状态="已开票")
        result, _ = service.run_action(entry["id"], "发起核对")
        self.assertEqual(result["status"], "核对中")
        result, _ = service.run_action(entry["id"], "确认结算")
        self.assertEqual(result["status"], "已收款")
        # pending/abnormal 沿用既有状态序列规则：仅终态“有争议”清 pending
        self.assertTrue(result["pending"])
        self.assertFalse(result["abnormal"])
        # 金额与开票状态全程未被服务改写
        self.assertEqual(result["应收金额"], Decimal("100.00"))
        self.assertEqual(result["开票状态"], "已开票")

    def test_dispute_marks_abnormal_flag_and_pending(self):
        entry = self._append()
        result, _ = SettlementService().run_action(entry["id"], "标记争议")
        self.assertEqual(result["status"], "有争议")
        self.assertFalse(result["pending"])


class SharedReadCaliberTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(store.rows(MODULE))

    def tearDown(self):
        store.rows(MODULE)[:] = self.original

    def test_export_matches_list_caliber_and_response_shape(self):
        service = SettlementService()
        list_items, total = service.list_entries(page=1, size=200)
        export_items, export_total = service.export_entries()
        self.assertEqual(total, export_total)
        self.assertEqual(list_items, export_items)
        for row in export_items:
            # 接口结构不变：原有键一个不少
            for key in ("id", "status", "pending", "abnormal", "结算单号",
                        "应收金额", "已收金额", "开票状态", "结算状态"):
                self.assertIn(key, row)

    def test_history_records_not_rewritten_by_reads(self):
        service = SettlementService()
        before = copy.deepcopy(store.rows(MODULE))
        service.list_entries(page=1, size=200)
        for entry in store.rows(MODULE):
            service.get_entry(int(entry["id"]))
        service.export_entries()
        self.assertEqual(store.rows(MODULE), before)

    def test_seed_doc_numbers_and_amounts_unchanged(self):
        before = copy.deepcopy(store.rows(MODULE))
        SettlementService().export_entries()
        for old, new in zip(before, store.rows(MODULE)):
            self.assertEqual(old["结算单号"], new["结算单号"])
            self.assertEqual(old["应收金额"], new["应收金额"])
            self.assertEqual(old["已收金额"], new["已收金额"])
            self.assertEqual(old["开票状态"], new["开票状态"])


if __name__ == "__main__":
    unittest.main()
