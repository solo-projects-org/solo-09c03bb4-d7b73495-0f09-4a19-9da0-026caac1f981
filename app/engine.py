"""折算引擎：按公告逐日把期初持仓折算到期末日。

规则（对应需求）：
- 公告区间为 (期初日, 期末日]，即期初日的次日起、期末日当天止。
- 同一交易日、同一个旧代码最多一条公告（入库前已校验）。
- 每个交易日先冻结当天起始余额，全部折算基于起始余额计算，
  新换得的份额当天不再折算（同日公告链 A -> B -> C 中，A 当天换到 B
  的份额不会再被 B -> C 折算），日内转换在收盘阶段统一结算。
- 全程使用 Decimal，中间不取整、不截断；份额以规范化字符串持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import groupby
from typing import Iterable

ZERO = Decimal(0)


@dataclass(frozen=True)
class Announcement:
    effective_date: str  # ISO 日期 YYYY-MM-DD
    old_code: str
    new_code: str
    ratio: Decimal  # 每 1 股旧代码换得 ratio 股新代码


@dataclass
class Lot:
    """一份按来源跟踪的持仓批次。"""

    origin_code: str  # 期初证券代码（折算来源）
    shares: Decimal
    current_code: str
    # 折算路径，例如 [("2026-02-01", "000001", "000002", Decimal("2"))]
    path: list[tuple[str, str, str, Decimal]] = field(default_factory=list)

    def __hash__(self) -> int:  # 以身份作哈希，允许作为字典键
        return id(self)

    def __eq__(self, other) -> bool:
        return self is other


def simulate(
    opening: Iterable[tuple[str, Decimal]],
    announcements: Iterable[Announcement],
    begin_date: str,
    end_date: str | None,
) -> list[Lot]:
    """把期初持仓 opening（(证券代码, 股数) 列表）按公告逐日折算到期末日。

    end_date 为 None 时窗口上不封顶（期末持仓尚未上传的预览场景），
    应用所有晚于期初日的公告。
    """
    lots = [
        Lot(origin_code=code, current_code=code, shares=shares)
        for code, shares in opening
    ]

    effective = sorted(
        (
            a
            for a in announcements
            if begin_date < a.effective_date
            and (end_date is None or a.effective_date <= end_date)
        ),
        key=lambda a: (a.effective_date, a.old_code),
    )

    for _day, day_anns in groupby(effective, key=lambda a: a.effective_date):
        day_anns = list(day_anns)
        # 冻结当日起始余额：零份额（已合并）批次不参与映射
        start_map: dict[str, Lot] = {
            lot.current_code: lot for lot in lots if lot.shares != ZERO
        }
        start_shares: dict[Lot, Decimal] = {
            lot: lot.shares for lot in start_map.values()
        }

        # 每个起始批次当日至多被一条公告折算，全部基于起始余额；
        # 日内新换得份额当天不再折算
        lot_ann: dict[Lot, Announcement] = {}
        for ann in day_anns:
            source = start_map.get(ann.old_code)
            if source is not None:
                lot_ann[source] = ann

        # 阶段一：在冻结的起始状态上规划每个批次的日终代码与份额，
        # 写入独立的日终槽位，期间不做任何批次间的就地合并/改写，
        # 避免同日换入的目标批次自己又被转走时连带折算新份额
        slots: dict[str, list[tuple[Lot, Decimal]]] = {}
        for lot in list(lots):
            ann = lot_ann.get(lot)
            if ann is None:
                end_code, end_shares = lot.current_code, lot.shares
            else:
                end_code = ann.new_code
                end_shares = start_shares[lot] * ann.ratio  # 中间不取整
                lot.path.append(
                    (ann.effective_date, ann.old_code, ann.new_code, ann.ratio)
                )
            slots.setdefault(end_code, []).append((lot, end_shares))

        # 阶段二：所有转换落定后按日终代码合并：每个代码保留一个有效批次，
        # 其余批次归零（折算路径仍保留在来源批次上，用于来源展示）
        for end_code, members in slots.items():
            keeper = members[0][0]
            keeper.current_code = end_code
            keeper.shares = sum((shares for _, shares in members), ZERO)
            for other, _shares in members[1:]:
                other.current_code = end_code
                other.shares = ZERO

    return lots


def trace_paths(
    opening_codes: Iterable[str],
    announcements: Iterable[Announcement],
    begin_date: str,
    end_date: str | None,
) -> dict[str, list[tuple[str, str, str]]]:
    """轻量代码级模拟：返回每个期初代码经历过的 (日期, 旧代码, 新代码) 路径。

    只跟踪代码、不计算股数，用于公告整表换报时判定哪些期初持仓
    可能受影响（受影响的才重新做完整折算）。
    """
    chains: dict[str, list[str]] = {code: [code] for code in opening_codes}
    paths: dict[str, list[tuple[str, str, str]]] = {
        code: [] for code in opening_codes
    }

    effective = sorted(
        (
            a
            for a in announcements
            if begin_date < a.effective_date
            and (end_date is None or a.effective_date <= end_date)
        ),
        key=lambda a: (a.effective_date, a.old_code),
    )

    for _day, day_anns in groupby(effective, key=lambda a: a.effective_date):
        # 冻结当日起始代码
        start_code = {origin: chain[-1] for origin, chain in chains.items()}
        moves: list[tuple[str, Announcement]] = []
        for ann in day_anns:
            for origin, code in start_code.items():
                if code == ann.old_code:
                    moves.append((origin, ann))
        for origin, ann in moves:
            chains[origin].append(ann.new_code)
            paths[origin].append(
                (ann.effective_date, ann.old_code, ann.new_code)
            )

    return paths
