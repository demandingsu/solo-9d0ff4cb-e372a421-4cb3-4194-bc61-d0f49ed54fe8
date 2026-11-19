"""CSV 解析与整批校验。

规则（任一不满足即拒收整批，不写库）：
- 表头必须包含投放号/采样序号/深度/水温/盐度五列（支持中英别名）；
- 单个文件只能属于一个投放号；
- 坏行：字段数不符、采样序号非整数、深度非有限数或为负、
  水温/盐度非空但不是有限数；
- 同一文件内采样序号重复即拒收；若异值会在错误信息中注明；
- 水温、盐度允许空单元格，空即缺测（NULL），参与缺测段统计。
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from typing import Optional

from .config import HEADER_ALIASES, REQUIRED_FIELDS


class BatchRejected(Exception):
    """整批拒收，errors 收集全部问题，便于一次反馈给数据员。"""

    def __init__(self, message: str, errors: list[str]):
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True)
class ParsedRow:
    row_index: int       # 数据行号，从 1 开始（不含表头）
    cast_id: str
    sample_no: int
    depth: float
    temperature: Optional[float]
    salinity: Optional[float]


@dataclass
class ParsedBatch:
    cast_id: str
    rows: list[ParsedRow]


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strict_int(text: str) -> Optional[int]:
    t = text.strip()
    if not t:
        return None
    try:
        # 拒绝 3.5、1e2 之类
        return int(t)
    except ValueError:
        return None


def _finite_float(text: str) -> tuple[bool, Optional[float]]:
    """返回 (是否合法, 值)。合法空值返回 (True, None)。"""
    t = text.strip()
    if t == "":
        return True, None
    try:
        v = float(t)
    except ValueError:
        return False, None
    if not math.isfinite(v):
        return False, None
    return True, v


def parse_csv(raw: bytes) -> ParsedBatch:
    if not raw or not raw.strip():
        raise BatchRejected("文件为空", ["上传内容为空"])

    text = decode_bytes(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise BatchRejected("文件为空", ["缺少表头"])

    # 表头映射
    seen_fields: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = HEADER_ALIASES.get(name.strip().lower())
        if key is None:
            continue
        if key in seen_fields:
            raise BatchRejected(
                "表头重复", [f"表头列 {name.strip()} 与第 {seen_fields[key] + 1} 列重复"]
            )
        seen_fields[key] = idx

    missing = [f for f in REQUIRED_FIELDS if f not in seen_fields]
    if missing:
        field_cn = {
            "cast_id": "投放号", "sample_no": "采样序号", "depth": "深度",
            "temperature": "水温", "salinity": "盐度",
        }
        missing_names = [f"{field_cn[f]}({f})" for f in missing]
        raise BatchRejected(
            "缺少必需列", [f"缺少必需列：{', '.join(missing_names)}（实际表头：{', '.join(header)}）"]
        )

    col = seen_fields
    ncols = len(header)
    errors: list[str] = []
    rows: list[ParsedRow] = []
    cast_id: Optional[str] = None

    for line_no, fields in enumerate(reader, start=1):
        if not fields or all(f.strip() == "" for f in fields):
            continue  # 跳过纯空行
        if len(fields) != ncols:
            errors.append(f"第 {line_no} 行：字段数 {len(fields)} 与表头 {ncols} 不一致")
            continue

        cid = fields[col["cast_id"]].strip()
        if not cid:
            errors.append(f"第 {line_no} 行：投放号为空")
            continue

        sample_no = _strict_int(fields[col["sample_no"]])
        if sample_no is None:
            errors.append(f"第 {line_no} 行：采样序号必须是整数（{fields[col['sample_no']]!r}）")
            continue
        if sample_no < 0:
            errors.append(f"第 {line_no} 行：采样序号不能为负（{sample_no}）")
            continue

        depth_ok, depth = _finite_float(fields[col["depth"]])
        if not depth_ok or depth is None:
            errors.append(f"第 {line_no} 行：深度必须是有限数字（{fields[col['depth']]!r}）")
            continue
        if depth < 0:
            errors.append(f"第 {line_no} 行：深度不能为负（{depth}）")
            continue

        temp_ok, temperature = _finite_float(fields[col["temperature"]])
        if not temp_ok:
            errors.append(
                f"第 {line_no} 行：水温必须是有限数字或留空（{fields[col['temperature']]!r}）"
            )
            continue

        sal_ok, salinity = _finite_float(fields[col["salinity"]])
        if not sal_ok:
            errors.append(
                f"第 {line_no} 行：盐度必须是有限数字或留空（{fields[col['salinity']]!r}）"
            )
            continue

        if cast_id is None:
            cast_id = cid
        elif cid != cast_id:
            errors.append(f"第 {line_no} 行：投放号 {cid} 与首行 {cast_id} 不一致，单文件仅限一个投放")
            continue

        rows.append(ParsedRow(line_no, cid, sample_no, depth, temperature, salinity))

    if not rows:
        errors.append("没有可用的数据行")

    # 同文件同序号重复检查（异值 / 同值分别提示，一律拒收）
    by_sample: dict[int, ParsedRow] = {}
    for r in rows:
        prev = by_sample.get(r.sample_no)
        if prev is None:
            by_sample[r.sample_no] = r
            continue
        same = (
            prev.depth == r.depth
            and prev.temperature == r.temperature
            and prev.salinity == r.salinity
        )
        if same:
            errors.append(
                f"采样序号 {r.sample_no} 在第 {prev.row_index}、{r.row_index} 行重复"
            )
        else:
            errors.append(
                f"采样序号 {r.sample_no} 在同文件内异值："
                f"第 {prev.row_index} 行 (深度={prev.depth}, 水温={prev.temperature}, 盐度={prev.salinity}) "
                f"vs 第 {r.row_index} 行 (深度={r.depth}, 水温={r.temperature}, 盐度={r.salinity})"
            )
    if errors:
        raise BatchRejected("CSV 校验未通过，整批拒收", errors)

    return ParsedBatch(cast_id=cast_id, rows=rows)
