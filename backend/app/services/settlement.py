"""检测结算业务规则：状态流转、字段校验与筛选口径都收在这里。

金额比较、开票状态与结算状态搭配、动作前置条件统一见 ``settlement_rules``，
列表、明细、导出三个读取入口共用同一套金额口径，不在这里各算一份。
"""
from __future__ import annotations

from typing import Any

from app.services.settlement_rules import (
    ACTION_RULES,
    STATUS_ORDER,
    Violation,
    check_action,
)
from app.store import store

MODULE = "settlement"
REQUIRED_FIELDS = ["结算单号", "委托单位", "结算周期"]
NEGATIVE_ACTIONS = []


class SettlementService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("结算单号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.present_entry(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self.present_entry(entry) if entry is not None else None

    def export_entries(self) -> tuple[list[dict[str, Any]], int]:
        """导出与列表共用读取口径：全量取数后走同一个 present_entry。"""
        items, total = self.list_entries(page=1, size=10000)
        return items, total

    @staticmethod
    def present_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """读取入口（列表/明细/导出）的唯一出口。

        金额取值与开票/结算搭配的解释统一在 settlement_rules，
        这里不重算金额，也不改写历史记录里的应收/已收金额与开票状态取值。
        """
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结算单 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于检测结算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        violation: Violation | None = check_action(action, entry)
        if violation is not None:
            return None, violation.message
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"结算单已{action}"
