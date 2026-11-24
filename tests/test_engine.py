"""折算引擎规则测试：逐日折算、同日链不再折算、边界日期、中间不取整。"""

from decimal import Decimal as D

from app.engine import Announcement, simulate, trace_paths


def ann(d, old, new, ratio):
    return Announcement(d, old, str(new), D(str(ratio)))


def by_code(lots):
    return {lot.current_code: lot.shares for lot in lots if lot.shares != 0}


def test_basic_split_within_window():
    # 000001 在 01-05 一拆二；000003 无公告
    lots = simulate(
        [("000001", D("100")), ("000003", D("7"))],
        [ann("2026-01-05", "000001", "000001", 2)],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots) == {"000001": D("200"), "000003": D("7")}


def test_date_window_is_exclusive_begin_inclusive_end():
    opening = [("000001", D("100"))]
    anns = [
        ann("2026-01-01", "000001", "000002", 2),  # 期初日当天，不生效
        ann("2026-02-01", "000001", "000002", 3),  # 期末日当天，生效
    ]
    lots = simulate(opening, anns, "2026-01-01", "2026-02-01")
    assert by_code(lots) == {"000002": D("300")}


def test_same_day_chain_new_shares_not_reconverted():
    # 同日 A->B (1:2) 与 B->C (1:5)：A 换得的 B 当天不再折算
    lots = simulate(
        [("A", D("100"))],
        [ann("2026-01-10", "A", "B", 2), ann("2026-01-10", "B", "C", 5)],
        "2026-01-01",
        "2026-01-31",
    )
    result = {l.current_code: l.shares for l in lots if l.shares != 0}
    assert result == {"B": D("200")}
    # 次日 B->C 才生效
    lots2 = simulate(
        [("A", D("100"))],
        [
            ann("2026-01-10", "A", "B", 2),
            ann("2026-01-11", "B", "C", 5),
        ],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots2) == {"C": D("1000")}


def test_same_day_chain_with_existing_balance_uses_start_balance():
    # 同日 A->B(1:2)、B->C(1:5)，且 B、C 本身有期初持仓：
    # 只有 B 的起始 10 股被折成 50 股 C，A 换入的 200 股 B 当天保留；
    # B 折入的 50 股 C 与 C 原 50 股合并为 100
    lots = simulate(
        [("A", D("100")), ("B", D("10")), ("C", D("50"))],
        [ann("2026-01-10", "A", "B", 2), ann("2026-01-10", "B", "C", 5)],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots) == {"B": D("200"), "C": D("100")}


def test_same_day_conversion_merges_then_converts_next_day():
    # 同日 A->B 并入起始 B；次日 B->C 时合并后的全部余额才参与折算
    lots = simulate(
        [("A", D("100")), ("B", D("10"))],
        [
            ann("2026-01-10", "A", "B", 2),
            ann("2026-01-11", "B", "C", 5),
        ],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots) == {"C": D("1050")}  # (200 + 10) * 5


def test_no_intermediate_rounding():
    # 无限小数比例：全程 Decimal 高精度保留，绝不向整数取整
    lots = simulate(
        [("A", D("100"))],
        [
            ann("2026-01-05", "A", "B", D("1") / D("3")),
            ann("2026-01-06", "B", "C", 3),
        ],
        "2026-01-01",
        "2026-01-31",
    )
    result = by_code(lots)["C"]
    assert result != D("99") and result != D("100")  # 未被取整
    assert abs(result - D("100")) < D("1e-25")       # 高精度近似

    # 精确小数链：100 * 1.5 * 2 = 300
    lots2 = simulate(
        [("A", D("100"))],
        [
            ann("2026-01-05", "A", "B", "1.5"),
            ann("2026-01-06", "B", "C", 2),
        ],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots2) == {"C": D("300")}

    # 小数份额原样保留，不四舍五入
    lots3 = simulate(
        [("A", D("100"))],
        [ann("2026-01-05", "A", "B", "0.125")],
        "2026-01-01",
        "2026-01-31",
    )
    assert by_code(lots3) == {"B": D("12.5")}


def test_merge_into_existing_code_and_provenance_kept():
    # A->B，B 自身有持仓：合并后 B 代码总额 240，两条来源路径均保留
    lots = simulate(
        [("A", D("100")), ("B", D("40"))],
        [ann("2026-01-05", "A", "B", 2)],
        "2026-01-01",
        "2026-01-31",
    )
    total_b = sum(l.shares for l in lots if l.current_code == "B")
    assert total_b == D("240")
    # 恰好一个有效批次承载合并份额，另一个归零但来源仍可追溯
    active = [l for l in lots if l.shares != 0]
    zeroed = [l for l in lots if l.shares == 0]
    assert len(active) == 1 and active[0].current_code == "B"
    assert {l.origin_code for l in lots} == {"A", "B"}
    a_lot = next(l for l in lots if l.origin_code == "A")
    assert a_lot.path[0][:3] == ("2026-01-05", "A", "B")
    assert (a_lot.shares == D("240")) or (a_lot in zeroed)


def test_trace_paths_same_day_chain():
    paths = trace_paths(
        ["A", "B"],
        [ann("2026-01-10", "A", "B", 2), ann("2026-01-10", "B", "C", 5)],
        "2026-01-01",
        "2026-01-31",
    )
    assert paths["A"] == [("2026-01-10", "A", "B")]
    assert paths["B"] == [("2026-01-10", "B", "C")]
