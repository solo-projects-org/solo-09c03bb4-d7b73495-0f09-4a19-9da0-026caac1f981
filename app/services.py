"""业务服务层：上传、整表换报、增量重算、净变动查询。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .csv_utils import CsvError, parse_announcements, parse_holdings
from .db import get_db
from .engine import Announcement, Lot, simulate, trace_paths


# ---------------------------------------------------------------- 小工具

def canon(d: Decimal) -> str:
    """Decimal -> 规范字符串（去尾零，中间不取整仅在运算阶段保证）。"""
    return format(d.normalize(), "f") if d != 0 else "0"


def _save_upload(raw: bytes, prefix: str) -> str:
    from flask import current_app

    directory = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(directory, exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex}.csv"
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(raw)
    return name


def _load_active_announcements(conn) -> list[Announcement]:
    batch = conn.execute(
        "SELECT id FROM announcement_batch WHERE status='active' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return []
    rows = conn.execute(
        "SELECT effective_date, old_code, new_code, ratio "
        "FROM announcement WHERE batch_id=? "
        "ORDER BY effective_date, old_code",
        (batch["id"],),
    ).fetchall()
    return [
        Announcement(
            effective_date=r["effective_date"],
            old_code=r["old_code"],
            new_code=r["new_code"],
            ratio=Decimal(r["ratio"]),
        )
        for r in rows
    ]


def _current_holding(conn, period: str):
    return conn.execute(
        "SELECT * FROM holding_file WHERE period=? AND is_current=1 "
        "ORDER BY id DESC LIMIT 1",
        (period,),
    ).fetchone()


def _holding_rows(conn, file_id: int) -> list[tuple[str, Decimal]]:
    rows = conn.execute(
        "SELECT code, shares FROM holding_row WHERE file_id=? ORDER BY code",
        (file_id,),
    ).fetchall()
    return [(r["code"], Decimal(r["shares"])) for r in rows]


def _latest_result(conn):
    return conn.execute(
        "SELECT * FROM result_batch ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _format_path(path_json: str) -> str:
    steps = json.loads(path_json)
    if not steps:
        return "无折算"
    parts = []
    for eff, old, new, ratio in steps:
        parts.append(f"{eff}: {old} → {new}（{ratio}:1）")
    return "；".join(parts)


# ---------------------------------------------------------------- 重算

def _persist_result(
    conn,
    lots: list[Lot],
    ann_batch_id: int,
    begin_file_id: int,
    end_file_id: Optional[int],
    begin_date: str,
    end_date: str,
    note: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO result_batch "
        "(announcement_batch_id, begin_file_id, end_file_id, begin_date, end_date, trigger_note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ann_batch_id, begin_file_id, end_file_id, begin_date, end_date, note),
    )
    result_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO result_lot "
        "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                result_id,
                lot.origin_code,
                lot.current_code,
                canon(lot.shares),
                json.dumps(
                    [(d, o, n, canon(r)) for d, o, n, r in lot.path],
                    ensure_ascii=False,
                ),
            )
            for lot in lots
        ],
    )
    return result_id


def _recompute_full(
    conn,
    announcements: list[Announcement],
    begin_file_id: int,
    end_file_id: Optional[int],
    begin_date: str,
    end_date: Optional[str],
    note: str,
) -> int:
    opening = _holding_rows(conn, begin_file_id)
    lots = simulate(opening, announcements, begin_date, end_date)
    active_batch = conn.execute(
        "SELECT id FROM announcement_batch WHERE status='active' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _persist_result(
        conn,
        lots,
        active_batch["id"],
        begin_file_id,
        end_file_id,
        begin_date,
        end_date,
        note,
    )


# ---------------------------------------------------------------- 持仓上传

@dataclass
class UploadResult:
    message: str
    result_id: Optional[int] = None


def upload_holding(period: str, as_of_date: str, filename: str, raw: bytes) -> UploadResult:
    """上传某一期完整持仓；同周期再次上传按整表替换处理。"""
    if period not in ("begin", "end"):
        raise CsvError("period 只能是 begin 或 end")
    rows = parse_holdings(raw)

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_name = _save_upload(raw, f"holding_{period}")
        cur = conn.execute(
            "INSERT INTO holding_file (period, as_of_date, filename, row_count) "
            "VALUES (?, ?, ?, ?)",
            (period, as_of_date, stored_name, len(rows)),
        )
        file_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO holding_row (file_id, code, shares) VALUES (?, ?, ?)",
            [(file_id, r.code, canon(r.shares)) for r in rows],
        )
        conn.execute(
            "UPDATE holding_file SET is_current=0 WHERE period=? AND id<>?",
            (period, file_id),
        )

        begin_file = _current_holding(conn, "begin")
        end_file = _current_holding(conn, "end")
        result_id = None
        if begin_file is not None:
            # 期末未上传时窗口上不封顶，期末日在结果批次中记空
            end_date = end_file["as_of_date"] if end_file else None
            if end_date is not None and end_date < begin_file["as_of_date"]:
                raise CsvError(
                    f"期末日 {end_date} 早于期初日 {begin_file['as_of_date']}，无法折算"
                )
            result_id = _recompute_full(
                conn,
                _load_active_announcements(conn),
                begin_file["id"],
                end_file["id"] if end_file else None,
                begin_file["as_of_date"],
                end_date,
                f"上传{ '期初' if period == 'begin' else '期末' }持仓触发全量重算",
            )
        conn.commit()
        return UploadResult(
            message=f"{period} 持仓已保存（{len(rows)} 行，来源文件 {filename}）",
            result_id=result_id,
        )
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 公告整表换报

def replace_announcements(filename: str, raw: bytes) -> tuple[str, Optional[int]]:
    """整表换报公告。

    流程：
    1. 先在事务外解析/校验新表，失败则只登记 failed 批次，原 active 表不动；
    2. 与现表完全相同则跳过，不产生新版本；
    3. 代码级轻量模拟找出“可能受改动影响”的期初持仓；
    4. 事务内建新批次、仅对受影响来源重算，其余来源沿用上一结果批次的行；
    5. 切换 active/superseded。任一步失败原表仍可查。
    """
    conn = get_db()

    # 1) 解析校验（不落库失败之外的数据）
    try:
        new_anns = parse_announcements(raw)
    except CsvError as exc:
        _record_failed_batch(conn, filename, exc)
        raise

    old_anns = _load_active_announcements(conn)

    def _sig(anns: list[Announcement]) -> set[tuple[str, str, str, str]]:
        return {
            (a.effective_date, a.old_code, a.new_code, canon(a.ratio))
            for a in anns
        }

    old_sig = _sig(old_anns)
    new_sig = _sig(new_anns)
    if old_sig == new_sig:
        return ("新公告表与当前生效表完全一致，未产生新版本。", None)

    begin_file = _current_holding(conn, "begin")
    if begin_file is None:
        # 期初持仓尚未上传：只换公告表，等持仓上传时再折算
        try:
            conn.execute("BEGIN IMMEDIATE")
            new_id = _insert_announcement_batch(conn, new_anns, filename, "期初持仓未上传，先换表")
            conn.execute(
                "UPDATE announcement_batch SET status='superseded', "
                "superseded_at=datetime('now') WHERE status='active' AND id<>?",
                (new_id,),
            )
            conn.commit()
            return (f"公告已换报（{len(new_anns)} 行）；期初持仓上传后自动折算。", None)
        except Exception:
            conn.rollback()
            raise

    end_file = _current_holding(conn, "end")
    end_date = end_file["as_of_date"] if end_file else None
    begin_date = begin_file["as_of_date"]
    if end_date is not None and end_date < begin_date:
        _record_failed_batch(
            conn, filename, CsvError(f"期末日 {end_date} 早于期初日 {begin_date}")
        )
        raise CsvError(f"期末日 {end_date} 早于期初日 {begin_date}，无法折算")

    opening = _holding_rows(conn, begin_file["id"])

    # 上一结果若基于不同的期初文件，所有来源都必须重算，不能复用旧行
    prev_result = _latest_result(conn)
    begin_changed = (
        prev_result is not None
        and prev_result["begin_file_id"] != begin_file["id"]
    )

    # 2) 代码级轻量模拟，定位受影响的期初来源
    new_paths = trace_paths(
        [code for code, _ in opening], new_anns, begin_date, end_date
    )
    old_paths = trace_paths(
        [code for code, _ in opening], old_anns, begin_date, end_date
    )
    changed_keys = old_sig.symmetric_difference(new_sig)
    if begin_changed:
        affected = {code for code, _ in opening}
    else:
        affected = {
            origin
            for origin in new_paths.keys() | old_paths.keys()
            if _path_touches(new_paths.get(origin, []), changed_keys)
            or _path_touches(old_paths.get(origin, []), changed_keys)
        }

    # 3) 只对受影响来源做完整股数模拟
    affected_opening = [(c, s) for c, s in opening if c in affected]
    fresh_lots = simulate(affected_opening, new_anns, begin_date, end_date)

    # 4) 落库：新批次 + 增量结果批次
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_name = _save_upload(raw, "announcement")
        new_batch_id = _insert_announcement_batch(
            conn, new_anns, filename, "公告整表换报", stored_name=stored_name
        )

        cur = conn.execute(
            "INSERT INTO result_batch "
            "(announcement_batch_id, begin_file_id, end_file_id, begin_date, end_date, trigger_note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_batch_id,
                begin_file["id"],
                end_file["id"] if end_file else None,
                begin_date,
                end_date,
                f"公告换报：仅重算 {len(affected)} / {len(opening)} 条期初持仓",
            ),
        )
        new_result_id = cur.lastrowid

        # 未受影响的来源：沿用上一结果批次的折算行
        # 仅当上一结果基于同一份期初文件时才可复用，否则全部重算
        reused = 0
        if prev_result is not None and not begin_changed:
            old_lots = conn.execute(
                "SELECT origin_code, end_code, adjusted_shares, path_json "
                "FROM result_lot WHERE result_batch_id=?",
                (prev_result["id"],),
            ).fetchall()
            reusable = [r for r in old_lots if r["origin_code"] not in affected]
            conn.executemany(
                "INSERT INTO result_lot "
                "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        new_result_id,
                        r["origin_code"],
                        r["end_code"],
                        r["adjusted_shares"],
                        r["path_json"],
                    )
                    for r in reusable
                ],
            )
            reused = len(reusable)

        conn.executemany(
            "INSERT INTO result_lot "
            "(result_batch_id, origin_code, end_code, adjusted_shares, path_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    new_result_id,
                    lot.origin_code,
                    lot.current_code,
                    canon(lot.shares),
                    json.dumps(
                        [(d, o, n, canon(r)) for d, o, n, r in lot.path],
                        ensure_ascii=False,
                    ),
                )
                for lot in fresh_lots
            ],
        )

        conn.execute(
            "UPDATE announcement_batch SET status='superseded', "
            "superseded_at=datetime('now') WHERE status='active' AND id<>?",
            (new_batch_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    msg = (
        f"公告整表换报成功（新表 {len(new_anns)} 行）；"
        f"受影响期初持仓 {len(affected)} 条已重算，{reused} 条沿用原折算结果。"
    )
    return msg, new_result_id


def _path_touches(
    path: list[tuple[str, str, str]], changed_keys: set[tuple]
) -> bool:
    # changed_keys 的元素是 (日期, 旧代码, 新代码, 比例)，路径只含前三段
    changed_triples = {(d, o, n) for (d, o, n, _r) in changed_keys}
    return any(step in changed_triples for step in path)



def _insert_announcement_batch(
    conn,
    anns: list[Announcement],
    filename: str,
    note: str,
    stored_name: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO announcement_batch (status, filename, row_count, note) "
        "VALUES ('active', ?, ?, ?)",
        (stored_name or filename, len(anns), note),
    )
    batch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO announcement (batch_id, effective_date, old_code, new_code, ratio) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (batch_id, a.effective_date, a.old_code, a.new_code, canon(a.ratio))
            for a in anns
        ],
    )
    return batch_id


def _record_failed_batch(conn, filename: str, exc: Exception) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO announcement_batch (status, filename, row_count, note) "
            "VALUES ('failed', ?, 0, ?)",
            (filename, f"换报失败，原表继续生效：{exc}"),
        )
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------- 查询

@dataclass
class NetChangeRow:
    end_code: str
    sources: str
    adjusted_shares: Decimal
    reported_shares: Decimal
    net_change: Decimal
    source_details: list[dict]


def get_net_change(result_id: Optional[int] = None) -> tuple[Optional[object], list[NetChangeRow]]:
    conn = get_db()
    result = conn.execute(
        "SELECT rb.*, ab.filename AS ann_filename "
        "FROM result_batch rb JOIN announcement_batch ab ON ab.id=rb.announcement_batch_id "
        + ("WHERE rb.id=?" if result_id is not None else "")
        + " ORDER BY rb.id DESC LIMIT 1",
        (result_id,) if result_id is not None else (),
    ).fetchone()
    if result is None:
        return None, []

    lots = conn.execute(
        "SELECT * FROM result_lot WHERE result_batch_id=? ORDER BY origin_code",
        (result["id"],),
    ).fetchall()

    reported: dict[str, Decimal] = {}
    if result["end_file_id"] is not None:
        for r in conn.execute(
            "SELECT code, shares FROM holding_row WHERE file_id=?",
            (result["end_file_id"],),
        ).fetchall():
            reported[r["code"]] = Decimal(r["shares"])

    # 按期末代码聚合折算来源
    grouped: dict[str, list[object]] = {}
    for lot in lots:
        grouped.setdefault(lot["end_code"], []).append(lot)

    codes = sorted(set(grouped.keys()) | set(reported.keys()))
    rows: list[NetChangeRow] = []
    for code in codes:
        lots_here = grouped.get(code, [])
        adjusted = sum(
            (Decimal(l["adjusted_shares"]) for l in lots_here), Decimal(0)
        )
        reported_value = reported.get(code, Decimal(0))  # 缺项按零
        details = [
            {
                "origin_code": l["origin_code"],
                "shares": Decimal(l["adjusted_shares"]),
                "path": _format_path(l["path_json"]),
            }
            for l in lots_here
        ]
        sources = "、".join(
            f"{d['origin_code']}（{canon(d['shares'])} 股）" for d in details
        ) or "—"
        rows.append(
            NetChangeRow(
                end_code=code,
                sources=sources,
                adjusted_shares=adjusted,
                reported_shares=reported_value,
                net_change=reported_value - adjusted,
                source_details=details,
            )
        )
    return result, rows


def list_history():
    conn = get_db()
    results = conn.execute(
        "SELECT rb.*, ab.status AS ann_status, ab.filename AS ann_filename "
        "FROM result_batch rb JOIN announcement_batch ab ON ab.id=rb.announcement_batch_id "
        "ORDER BY rb.id DESC"
    ).fetchall()
    batches = conn.execute(
        "SELECT * FROM announcement_batch ORDER BY id DESC"
    ).fetchall()
    holdings = conn.execute(
        "SELECT * FROM holding_file ORDER BY id DESC"
    ).fetchall()
    return results, batches, holdings
