"""检测结算共用判定：金额口径、开票/结算状态搭配与动作前置条件。

发起核对、确认结算、标记争议三个入口以及列表/明细/导出读取全部走这一份实现，
不再各写一套金额与状态比较。所有函数只读入参，不改写任何历史结算记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# 结算单字段
F_RECEIVABLE = "应收金额"
F_RECEIVED = "已收金额"
F_INVOICE = "开票状态"
F_STATUS = "status"

STATUS_ORDER = ["待核对", "核对中", "已确认", "已收款", "有争议"]
ACTION_RULES: dict[str, str] = {
    "发起核对": "核对中",
    "确认结算": "已收款",
    "标记争议": "有争议",
}
DISPUTED_STATUS = "有争议"

# 开票状态取值；历史数据里的占位文本视为未登记，不参与收款/开票搭配校验
INVOICE_NOT_ISSUED = "未开票"
INVOICE_ISSUED = "已开票"
KNOWN_INVOICE_STATUSES = (INVOICE_NOT_ISSUED, INVOICE_ISSUED)


@dataclass(frozen=True)
class Violation:
    """一条判定不通过的原因；code 给调用方做分支，message 直接面向使用者。"""

    code: str
    message: str


def _to_amount(value: Any) -> Decimal | None:
    """统一金额口径：空串/空白/None/非数字一律视为未登记，数字用 Decimal 精确比较。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class AmountView:
    """一条结算单的金额视图，三个入口与导出共用同一份取值。"""

    receivable: Decimal | None
    received: Decimal | None

    @property
    def receivable_missing(self) -> bool:
        return self.receivable is None

    @property
    def overpaid(self) -> bool:
        """已收超应收：应收未登记时无法比较，不算超收。"""
        if self.receivable is None:
            return False
        received = self.received or Decimal("0")
        return received > self.receivable

    @property
    def paid_in_full(self) -> bool:
        """款已收齐：必须有应收金额，且已收等于应收（缺失已收按 0 计）。"""
        if self.receivable is None:
            return False
        return (self.received or Decimal("0")) == self.receivable


def amount_view(entry: dict[str, Any]) -> AmountView:
    return AmountView(
        receivable=_to_amount(entry.get(F_RECEIVABLE)),
        received=_to_amount(entry.get(F_RECEIVED)),
    )


def _is_issued(invoice_status: Any) -> bool:
    return str(invoice_status or "").strip() == INVOICE_ISSUED


def check_action(action: str, entry: dict[str, Any]) -> Violation | None:
    """三个动作入口共用的前置判定；返回 None 表示通过。

    - 发起核对：应收金额必须已登记；
    - 确认结算：应收必须已登记、已收不能超应收、必须收齐，
      且收款/开票搭配必须一致（已收款要已开票、已开票也要已收款）；
    - 标记争议：不允许对已经处于争议状态的结算单重复标记。
    """
    amounts = amount_view(entry)

    # 标记争议的前提：当前不在争议状态，避免重复标记争议
    if action == "标记争议":
        if entry.get(F_STATUS) == DISPUTED_STATUS:
            return Violation("duplicate_dispute", "结算单已处于争议状态，不能重复标记争议")
        return None

    # 金额类动作共用：应收金额为空（含空白/非数字）一律先拦下
    if action in ("发起核对", "确认结算") and amounts.receivable_missing:
        return Violation("receivable_missing", "应收金额未登记，无法进行金额核对")

    if action == "确认结算":
        received = amounts.received or Decimal("0")
        if amounts.overpaid:
            return Violation(
                "received_exceeds_receivable",
                f"已收金额 {received} 超过应收金额 {amounts.receivable}，不能确认结算",
            )
        # 开票状态与结算状态的搭配：收款闭环与开票闭环必须同时成立。
        # 作为同一条搭配规则整体判定，避免被金额比较的分支抢先吞掉。
        invoice_status = str(entry.get(F_INVOICE) or "").strip()
        if invoice_status in KNOWN_INVOICE_STATUSES:
            issued = _is_issued(invoice_status)
            if amounts.paid_in_full and not issued:
                return Violation(
                    "paid_without_invoice",
                    "结算单已收款但尚未开票，开票状态与结算状态不匹配，不能确认结算",
                )
            if issued and not amounts.paid_in_full:
                return Violation(
                    "invoiced_without_payment",
                    "结算单已开票但款项未收齐，开票状态与结算状态不匹配，不能确认结算",
                )
        if not amounts.paid_in_full:
            return Violation(
                "received_not_in_full",
                f"已收金额 {received} 与应收金额 {amounts.receivable} 不一致，不能确认结算",
            )

    return None
