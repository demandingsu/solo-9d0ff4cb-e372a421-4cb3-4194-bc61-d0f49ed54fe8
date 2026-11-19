"""剖面计算：下行过滤 + 整米深度段聚合。

下行过滤规则（严格按原始 CSV 行序，即采样序号顺序）：
- 维护“此前最大深度”；
- 当前行深度严格大于该最大值才保留，并刷新最大值；
- 否则判为回升/重复深度行，记入排除清单（reason_code=upcast）。
  深度相等属于重复深度，同样排除，避免同一水层读数混入。
整米段：bin = floor(depth)，段 [bin, bin+1)。
- 温盐均值仅对非缺测值求平均；缺测不计入计数；
- 段内完全没有有效读数（含该段行的温盐全为空）标注缺测；
- 剖面覆盖范围内没有任何保留行的整米段也列为缺测段。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .parsing import ParsedRow


@dataclass
class Exclusion:
    raw_row_id: int           # 由服务层在落库后回填，这里暂用 -1
    row_index: int
    sample_no: int
    depth: float
    reason_code: str
    reason: str
    detail: str


@dataclass
class BinStat:
    depth_bin: int
    row_count: int
    temp_mean: Optional[float]
    salinity_mean: Optional[float]
    temp_count: int
    salinity_count: int
    all_missing: bool


@dataclass
class ProfileResult:
    retained: list[ParsedRow] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    bins: list[BinStat] = field(default_factory=list)
    missing_bins: list[int] = field(default_factory=list)
    max_depth: float = 0.0
    retained_count: int = 0
    excluded_count: int = 0


def compute_profile(rows: list[ParsedRow]) -> ProfileResult:
    result = ProfileResult()
    max_depth: Optional[float] = None

    # 1) 下行过滤：只保留深度首次超过此前最大值的读数
    for row in rows:
        if max_depth is None or row.depth > max_depth:
            result.retained.append(row)
            max_depth = row.depth
        else:
            reason_code = "upcast"
            if row.depth == max_depth:
                reason = "重复深度行：与此前最大深度相等，未严格下潜"
                detail = f"当前深度 {row.depth} = 此前最大深度 {max_depth}"
            else:
                reason = "回升行：深度未超过此前最大深度，判定为回升读数"
                detail = f"当前深度 {row.depth} ≤ 此前最大深度 {max_depth}"
            result.exclusions.append(
                Exclusion(
                    raw_row_id=-1,
                    row_index=row.row_index,
                    sample_no=row.sample_no,
                    depth=row.depth,
                    reason_code=reason_code,
                    reason=reason,
                    detail=detail,
                )
            )

    result.max_depth = max_depth if max_depth is not None else 0.0
    result.retained_count = len(result.retained)
    result.excluded_count = len(result.exclusions)

    # 2) 按整米段聚合保留行
    groups: dict[int, dict[str, list[float]]] = {}
    for row in result.retained:
        b = math.floor(row.depth)
        g = groups.setdefault(b, {"temp": [], "sal": []})
        if row.temperature is not None:
            g["temp"].append(row.temperature)
        if row.salinity is not None:
            g["sal"].append(row.salinity)

    if not groups:
        return result

    lo, hi = min(groups), max(groups)
    for b in range(lo, hi + 1):
        g = groups.get(b)
        if g is None:
            result.missing_bins.append(b)
            result.bins.append(
                BinStat(b, 0, None, None, 0, 0, all_missing=True)
            )
            continue
        temps, sals = g["temp"], g["sal"]
        bin_rows = [r for r in result.retained if math.floor(r.depth) == b]
        row_count = len(bin_rows)
        all_missing = not temps and not sals
        result.bins.append(
            BinStat(
                depth_bin=b,
                row_count=row_count,
                temp_mean=(sum(temps) / len(temps)) if temps else None,
                salinity_mean=(sum(sals) / len(sals)) if sals else None,
                temp_count=len(temps),
                salinity_count=len(sals),
                all_missing=all_missing,
            )
        )
        if all_missing:
            result.missing_bins.append(b)

    return result
