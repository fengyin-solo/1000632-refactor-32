"""检测结算业务规则：状态流转、字段校验与筛选口径都收在这里。

金额与开票状态的判定全模块只有一份实现：应收/已收的比较、开票状态与结算状态的
搭配、标记争议的前提条件都走下面的共用函数。发起核对、确认结算、标记争议三个
入口和导出看到的是同一套金额口径；派生字段只在展示层计算，存储的历史结算记录
不会被改写。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.store import store

MODULE = "settlement"
REQUIRED_FIELDS = ["结算单号", "委托单位", "结算周期"]
STATUS_ORDER = ["待核对", "核对中", "已确认", "已收款", "有争议"]
ACTION_RULES = {"发起核对": "核对中", "确认结算": "已收款", "标记争议": "有争议"}
NEGATIVE_ACTIONS = []

# ---- 金额口径：应收/已收/未收与收款状态的唯一判定处，列表、详情、导出与动作校验共用 ----
RECEIVABLE_FIELD = "应收金额"
RECEIVED_FIELD = "已收金额"

RECEIPT_EMPTY = "金额待补录"   # 应收金额为空，无法比较
RECEIPT_NONE = "未收款"
RECEIPT_PARTIAL = "部分收款"
RECEIPT_SETTLED = "已收讫"
RECEIPT_OVER = "超额收款"      # 已收超应收

CENT = Decimal("0.01")


def parse_amount(value: Any) -> Decimal | None:
    """把金额解析到分位的 Decimal；None、空串或无法解析时返回 None（应收金额为空的情形）。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(CENT)


def amount_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """应收/已收/未收与收款状态：三个动作入口与导出共用的同一份金额口径。

    应收金额为空时收款状态为「金额待补录」，未收金额给 None 而不是硬算一个数；
    已收超过应收时状态为「超额收款」，未收金额为负，差额方向一目了然。
    """
    receivable = parse_amount(entry.get(RECEIVABLE_FIELD))
    received = parse_amount(entry.get(RECEIVED_FIELD))
    if received is None:
        received = Decimal("0.00")
    if receivable is None:
        return {"未收金额": None, "收款状态": RECEIPT_EMPTY}
    outstanding = receivable - received
    if received <= 0:
        status = RECEIPT_NONE
    elif outstanding > 0:
        status = RECEIPT_PARTIAL
    elif outstanding == 0:
        status = RECEIPT_SETTLED
    else:
        status = RECEIPT_OVER
    return {"未收金额": float(outstanding), "收款状态": status}


# ---- 开票状态与结算状态的搭配：同一张搭配表驱动动作校验与展示核对 ----
INVOICE_STATUSES = ("未开票", "部分开票", "已开票", "无需开票")
INVOICE_PAIRING = {
    "待核对": ("未开票",),
    "核对中": ("未开票",),
    "已确认": INVOICE_STATUSES,
    "已收款": ("已开票", "无需开票"),
    "有争议": INVOICE_STATUSES,
}


def normalize_invoice_status(value: Any) -> str | None:
    """只认标准开票状态；未登记或历史遗留的无法识别取值返回 None，不妄判。"""
    text = str(value or "").strip()
    return text if text in INVOICE_STATUSES else None


def invoice_pairing_problem(entry: dict[str, Any], target_status: str) -> str | None:
    """开票状态与目标结算状态不搭时给出原因；开票状态无法识别时不拦。"""
    invoice = normalize_invoice_status(entry.get("开票状态"))
    if invoice is None:
        return None
    allowed = INVOICE_PAIRING.get(target_status, ())
    if invoice not in allowed:
        return f"开票状态为「{invoice}」，与「{target_status}」不搭，请先核对开票信息"
    return None


def invoice_pairing_flag(entry: dict[str, Any]) -> str:
    """展示层的核对结论：开票状态与当前结算状态是否搭得上。"""
    invoice = normalize_invoice_status(entry.get("开票状态"))
    if invoice is None:
        return "开票状态未登记"
    allowed = INVOICE_PAIRING.get(str(entry.get("status") or ""), ())
    return "一致" if invoice in allowed else "与结算状态不搭"


# ---- 标记争议的前提：已收款或已有争议的单子不能再标（重复标记会被拦下） ----
def dispute_problem(entry: dict[str, Any]) -> str | None:
    status = entry.get("status")
    if status == "有争议":
        return "结算单已处于「有争议」状态，请勿重复标记争议"
    if status == "已收款":
        return "结算单已收款，不能再标记争议，请走退款或更正流程"
    return None


def amount_problem(entry: dict[str, Any], action: str) -> str | None:
    """动作前的金额校验：应收为空不核对不结算，已收超应收不结算。"""
    if action == "标记争议":
        return None  # 争议往往正是因为金额对不上，不能用金额口径拦争议
    status = amount_snapshot(entry)["收款状态"]
    if status == RECEIPT_EMPTY:
        return f"应收金额未登记，请先补录金额再{action}"
    if action == "确认结算" and status != RECEIPT_SETTLED:
        if status == RECEIPT_OVER:
            return "已收金额超过应收金额，请核实差额后再确认结算"
        return "已收金额尚未收齐，不能确认结算"
    return None


def action_problem(entry: dict[str, Any], action: str) -> str | None:
    """发起核对、确认结算、标记争议共用的前提校验入口，三个入口不再各写一遍。"""
    if action == "标记争议":
        return dispute_problem(entry)
    target = ACTION_RULES[action]
    return amount_problem(entry, action) or invoice_pairing_problem(entry, target)


def present_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """列表、详情、导出共用的展示口径：复制一份再补派生字段，不改写存储的历史记录。"""
    view = dict(entry)
    view.update(amount_snapshot(entry))
    view["开票核对"] = invoice_pairing_flag(entry)
    return view


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
        return [present_entry(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return present_entry(entry) if entry is not None else None

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
        return present_entry(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结算单 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于检测结算可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        problem = action_problem(entry, action)
        if problem:
            return None, problem
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return present_entry(entry), f"结算单已{action}"
