"""CSV 解析与校验。

持仓 CSV 列：证券代码、股数
公告 CSV 列：生效日、旧代码、新代码、新旧股数比（每 1 股旧代码换得的新代码股数）
表头允许中文列名及常见英文别名；证券代码一律按文本处理（保留前导零）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from .engine import Announcement

HOLDING_ALIASES = {
    "证券代码": "code",
    "代码": "code",
    "证券代码 ": "code",
    "code": "code",
    "symbol": "code",
    "股数": "shares",
    "持股数": "shares",
    "持股数量": "shares",
    "数量": "shares",
    "shares": "shares",
    "quantity": "shares",
    "qty": "shares",
}

ANNOUNCEMENT_ALIASES = {
    "生效日": "effective_date",
    "生效日期": "effective_date",
    "date": "effective_date",
    "effective_date": "effective_date",
    "旧代码": "old_code",
    "原代码": "old_code",
    "old_code": "old_code",
    "from_code": "old_code",
    "新代码": "new_code",
    "换入代码": "new_code",
    "new_code": "new_code",
    "to_code": "new_code",
    "新旧股数比": "ratio",
    "换股比例": "ratio",
    "比例": "ratio",
    "ratio": "ratio",
}


class CsvError(ValueError):
    """CSV 内容不符合要求。"""


@dataclass
class HoldingRow:
    code: str
    shares: Decimal


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvError("文件编码无法识别，请使用 UTF-8 或 GB18030 编码")


def _read_rows(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvError("CSV 缺少表头")
    headers = [h.strip() for h in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        if raw_row is None:
            continue
        row = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
        }
        # 跳过整行空白
        if not any(v for v in row.values() if v):
            continue
        rows.append(row)
    return rows, headers


def _map_headers(headers: list[str], aliases: dict[str, str], need: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for h in headers:
        key = aliases.get(h)
        if key and key not in mapping:
            mapping[key] = h
    missing = [k for k in need if k not in mapping]
    if missing:
        raise CsvError(
            f"表头缺少必需列：{', '.join(missing)}；实际表头：{', '.join(headers)}"
        )
    return mapping


def _parse_decimal(value: str, field_name: str, line: int) -> Decimal:
    if value is None or value == "":
        raise CsvError(f"第 {line} 行 {field_name} 为空")
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise CsvError(f"第 {line} 行 {field_name} 不是合法数字：{value}")
    if not d.is_finite():
        raise CsvError(f"第 {line} 行 {field_name} 不是有限数字：{value}")
    return d


def parse_holdings(raw: bytes) -> list[HoldingRow]:
    rows, headers = _read_rows(raw)
    if not rows:
        raise CsvError("持仓 CSV 没有数据行")
    mapping = _map_headers(headers, HOLDING_ALIASES, ["code", "shares"])
    out: list[HoldingRow] = []
    seen: dict[str, int] = {}
    for i, row in enumerate(rows, start=2):  # 表头占第 1 行
        code = row.get(mapping["code"], "") or ""
        code = code.strip()
        if not code:
            raise CsvError(f"第 {i} 行证券代码为空")
        shares = _parse_decimal(row.get(mapping["shares"], ""), "股数", i)
        if shares < 0:
            raise CsvError(f"第 {i} 行股数不能为负数：{shares}")
        if code in seen:
            raise CsvError(
                f"证券代码 {code} 在第 {seen[code]} 行与第 {i} 行重复，请先合并"
            )
        seen[code] = i
        out.append(HoldingRow(code=code, shares=shares))
    return out


def _parse_iso_date(value: str, line: int) -> str:
    value = value.strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise CsvError(f"第 {line} 行生效日不是 YYYY-MM-DD 日期：{value}")


def parse_announcements(raw: bytes) -> list[Announcement]:
    rows, headers = _read_rows(raw)
    if not rows:
        raise CsvError("公告 CSV 没有数据行")
    mapping = _map_headers(
        headers,
        ANNOUNCEMENT_ALIASES,
        ["effective_date", "old_code", "new_code", "ratio"],
    )
    out: list[Announcement] = []
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows, start=2):
        eff = _parse_iso_date(row.get(mapping["effective_date"], ""), i)
        old_code = (row.get(mapping["old_code"], "") or "").strip()
        new_code = (row.get(mapping["new_code"], "") or "").strip()
        if not old_code:
            raise CsvError(f"第 {i} 行旧代码为空")
        if not new_code:
            raise CsvError(f"第 {i} 行新代码为空")
        ratio = _parse_decimal(row.get(mapping["ratio"], ""), "新旧股数比", i)
        if ratio <= 0:
            raise CsvError(f"第 {i} 行新旧股数比必须为正数：{ratio}")
        key = (eff, old_code)
        if key in seen:
            raise CsvError(
                f"第 {i} 行：生效日 {eff}、旧代码 {old_code} 存在多条公告，"
                "同日每个旧代码最多一条"
            )
        seen.add(key)
        out.append(
            Announcement(
                effective_date=eff,
                old_code=old_code,
                new_code=new_code,
                ratio=ratio,
            )
        )
    out.sort(key=lambda a: (a.effective_date, a.old_code))
    return out


def parse_form_date(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise CsvError(f"{field_name}不能为空")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise CsvError(f"{field_name}不是 YYYY-MM-DD 日期：{value}")
