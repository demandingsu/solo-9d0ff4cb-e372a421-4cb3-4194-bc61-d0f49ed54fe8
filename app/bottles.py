"""瓶样交接单服务层：批次版本化、关联原始行、平行瓶资格与交接单生成。

业务规则
- 瓶样 CSV 含 投放号 / 瓶号 / 采样序号（击发采样序号）/ 实验室盐度；
  整批校验在事务外完成，拒收不改动任何既有数据。
- 按采样序号关联该投放“现行已发布原始版”的原始采样行：
  即便采瓶发生在回升段（该行在下行剖面中被排除），也直接采用原始行的
  深度/温/盐，绝不套用下行整米均值；无法关联（序号在原始版中不存在）
  的瓶子列出原因且不可交付。
- 同采样序号的平行瓶：实验室盐度极差 > 本批容差 => 整组待核（pending），
  组内全部瓶子不进入可交付清单；更正其中一瓶需整批重传，组资格随整批重算。
- 瓶样整批更正（同投放重传）或投放原始版重传后：旧交接单留存但立即停止
  现行下载（is_current=0）；原始新版发布后通过“重新生成”才恢复现行单。
- 所有写操作在单个 BEGIN IMMEDIATE 事务内完成，交接单头与全部明细行
  要么全部落库要么整体回滚，不留半张清单；现行清单必然同时对应同一个
  已提交瓶样批次与同一个已发布原始版。
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import sqlite3
from typing import Any, Optional

from .db import write_lock
from .parsing import BottleBatch, BottleRow, parse_bottle_csv
from .services import _fmt, _http, _now


# ---------------------------------------------------------------- 入库

def upload_bottle_batch(
    conn: sqlite3.Connection, raw: bytes, filename: str, tolerance: float
) -> dict[str, Any]:
    # 事务外解析：坏行/冲突直接拒收，库内数据不受影响
    batch: BottleBatch = parse_bottle_csv(raw)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise _http(422, f"本批容差必须是非负有限数（收到 {tolerance!r}）")

    digest = hashlib.sha256(raw).hexdigest()
    now = _now()

    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cast = conn.execute(
                "SELECT * FROM casts WHERE cast_id=?", (batch.cast_id,)
            ).fetchone()
            if cast is None:
                conn.rollback()
                raise _http(
                    409,
                    f"投放 {batch.cast_id} 尚无原始采样数据，请先上传原始 CSV "
                    "并确认发布后再生成瓶样交接单",
                )

            dup = conn.execute(
                "SELECT id, batch_no FROM bottle_batches WHERE cast_id=? AND file_sha256=?",
                (batch.cast_id, digest),
            ).fetchone()
            if dup is not None:
                conn.rollback()
                raise _http(
                    409,
                    f"该瓶样文件与第 {dup['batch_no']} 批内容完全相同（sha256 一致），无需重传",
                )

            row = conn.execute(
                "SELECT COALESCE(MAX(batch_no), 0) AS m FROM bottle_batches WHERE cast_id=?",
                (batch.cast_id,),
            ).fetchone()
            batch_no = row["m"] + 1

            cur = conn.execute(
                "INSERT INTO bottle_batches(cast_id, batch_no, tolerance, file_sha256,"
                " filename, row_count, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (batch.cast_id, batch_no, tolerance, digest, filename,
                 len(batch.rows), now),
            )
            batch_id = cur.lastrowid
            for r in batch.rows:
                conn.execute(
                    "INSERT INTO bottle_rows(batch_id, row_index, bottle_no, sample_no,"
                    " lab_salinity) VALUES (?, ?, ?, ?, ?)",
                    (batch_id, r.row_index, r.bottle_no, r.sample_no, r.lab_salinity),
                )

            # 旧批次立即 superseded；旧交接单留存但停止现行下载
            conn.execute(
                "UPDATE bottle_batches SET status='superseded', superseded_at=?"
                " WHERE cast_id=? AND id != ? AND status='active'",
                (now, batch.cast_id, batch_id),
            )
            conn.execute(
                "UPDATE handover_sheets SET is_current=0, superseded_at=?,"
                " invalidate_reason='bottle_correction'"
                " WHERE cast_id=? AND is_current=1",
                (now, batch.cast_id),
            )

            sheet: Optional[dict[str, Any]] = None
            # 仅当存在现行已发布原始版时才同步生成交接单
            if cast["current_version_id"] is not None:
                sheet = _create_sheet(
                    conn, batch.cast_id, batch_id, cast["current_version_id"],
                    tolerance, batch.rows, now,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "cast_id": batch.cast_id,
        "batch_id": batch_id,
        "batch_no": batch_no,
        "tolerance": tolerance,
        "filename": filename,
        "file_sha256": digest,
        "row_count": len(batch.rows),
        "sheet_id": sheet["id"] if sheet else None,
        "sheet_created": sheet is not None,
        "created_at": now,
        **_counts(sheet),
    }


def regenerate_handover(conn: sqlite3.Connection, cast_id: str) -> dict[str, Any]:
    """原始新版发布后重新生成现行交接单（也用于补偿缺失的现行单）。"""
    now = _now()
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cast = conn.execute(
                "SELECT * FROM casts WHERE cast_id=?", (cast_id,)
            ).fetchone()
            if cast is None:
                conn.rollback()
                raise _http(404, f"投放 {cast_id} 不存在")

            latest_batch = conn.execute(
                "SELECT * FROM bottle_batches WHERE cast_id=?"
                " ORDER BY batch_no DESC LIMIT 1",
                (cast_id,),
            ).fetchone()
            if latest_batch is None:
                conn.rollback()
                raise _http(409, f"投放 {cast_id} 尚未上传瓶样 CSV，无法生成交接单")
            if latest_batch["status"] != "active":
                conn.rollback()
                raise _http(409, "瓶样批次状态异常，请重新上传瓶样 CSV 后再生成")

            raw_version_id = cast["current_version_id"]
            if raw_version_id is None:
                conn.rollback()
                raise _http(
                    409,
                    f"投放 {cast_id} 的原始版重传后尚未确认发布，"
                    "新版发布后才能重新生成交接单",
                )
            version = conn.execute(
                "SELECT id, status, version_no FROM raw_versions WHERE id=?",
                (raw_version_id,),
            ).fetchone()
            if version is None or version["status"] != "published":
                conn.rollback()
                raise _http(
                    409, "现行原始版未处于已发布状态，请先确认发布后再生成交接单"
                )

            existing = conn.execute(
                "SELECT * FROM handover_sheets WHERE batch_id=? AND raw_version_id=?",
                (latest_batch["id"], raw_version_id),
            ).fetchone()
            if existing is not None:
                if existing["is_current"] == 1:
                    conn.rollback()
                    return get_handover_detail(conn, existing["id"])
                conn.rollback()
                raise _http(
                    409,
                    "同依据的交接单已停止现行下载，请重新上传瓶样批次或等待新原始版发布",
                )

            # 防御性：任何残留现行单先停用，保证全表至多一张现行单
            conn.execute(
                "UPDATE handover_sheets SET is_current=0, superseded_at=?,"
                " invalidate_reason='regenerate' WHERE cast_id=? AND is_current=1",
                (now, cast_id),
            )

            bottle_rows = conn.execute(
                "SELECT * FROM bottle_rows WHERE batch_id=? ORDER BY row_index",
                (latest_batch["id"],),
            ).fetchall()
            parsed = [
                BottleRow(r["row_index"], cast_id, r["bottle_no"],
                          r["sample_no"], r["lab_salinity"])
                for r in bottle_rows
            ]
            sheet = _create_sheet(
                conn, cast_id, latest_batch["id"], raw_version_id,
                latest_batch["tolerance"], parsed, now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return get_handover_detail(conn, sheet["id"])


def invalidate_current_sheets_on_raw_reupload(
    conn: sqlite3.Connection, cast_id: str, now: str
) -> None:
    """原始版重传事务内调用：旧交接单留存但立即停止现行下载。"""
    conn.execute(
        "UPDATE handover_sheets SET is_current=0, superseded_at=?,"
        " invalidate_reason='raw_reupload' WHERE cast_id=? AND is_current=1",
        (now, cast_id),
    )


# ---------------------------------------------------------------- 建单

def _create_sheet(
    conn: sqlite3.Connection,
    cast_id: str,
    batch_id: int,
    raw_version_id: int,
    tolerance: float,
    bottle_rows: list[BottleRow],
    now: str,
) -> dict[str, Any]:
    """在调用方事务内插入交接单头与全部明细行（同生共死，不留半张）。"""
    # 原始行按采样序号索引（同版内序号唯一，上传整批校验已保证）
    raw_map = {
        r["sample_no"]: r
        for r in conn.execute(
            "SELECT * FROM raw_rows WHERE version_id=?", (raw_version_id,)
        ).fetchall()
    }
    # 回升/重复深度排除行：即便采瓶在回升段，仍采用原始行而非整米均值
    excluded = {
        e["raw_row_id"]: e
        for e in conn.execute(
            "SELECT * FROM excluded_rows WHERE version_id=?", (raw_version_id,)
        ).fetchall()
    }
    version = conn.execute(
        "SELECT version_no FROM raw_versions WHERE id=?", (raw_version_id,)
    ).fetchone()
    version_no = version["version_no"]

    # 平行瓶分组与盐度极差
    groups: dict[int, list[BottleRow]] = {}
    for r in bottle_rows:
        groups.setdefault(r.sample_no, []).append(r)
    spread = {
        no: max(x.lab_salinity for x in g) - min(x.lab_salinity for x in g)
        for no, g in groups.items()
    }
    pending_groups = {no for no, d in spread.items() if d > tolerance}

    cur = conn.execute(
        "INSERT INTO handover_sheets(cast_id, batch_id, raw_version_id, tolerance,"
        " is_current, delivered_count, pending_count, unlinked_count, created_at)"
        " VALUES (?, ?, ?, ?, 1, 0, 0, 0, ?)",
        (cast_id, batch_id, raw_version_id, tolerance, now),
    )
    sheet_id = cur.lastrowid

    delivered = pending_cnt = unlinked_cnt = 0
    for r in bottle_rows:
        raw = raw_map.get(r.sample_no)
        group_pending = r.sample_no in pending_groups
        if raw is None:
            linked = False
            unlinked_reason = (
                f"已发布原始版 v{version_no} 中不存在采样序号 {r.sample_no}，无法关联"
            )
            conn.execute(
                "INSERT INTO handover_rows(sheet_id, row_index, bottle_no, sample_no,"
                " lab_salinity, linked, raw_row_id, raw_row_index, depth, ctd_temperature,"
                " ctd_salinity, is_upcast, upcast_reason, parallel_size, salinity_spread,"
                " group_status, deliverable, unlinked_reason)"
                " VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, 0, NULL,"
                " ?, ?, ?, 0, ?)",
                (sheet_id, r.row_index, r.bottle_no, r.sample_no, r.lab_salinity,
                 len(groups[r.sample_no]), spread[r.sample_no],
                 "pending" if group_pending else "ok", unlinked_reason),
            )
            unlinked_cnt += 1
            if group_pending:
                pending_cnt += 1
            continue

        ex = excluded.get(raw["id"])
        is_upcast = ex is not None
        deliverable = not group_pending  # 能关联且组资格通过才可交付
        conn.execute(
            "INSERT INTO handover_rows(sheet_id, row_index, bottle_no, sample_no,"
            " lab_salinity, linked, raw_row_id, raw_row_index, depth, ctd_temperature,"
            " ctd_salinity, is_upcast, upcast_reason, parallel_size, salinity_spread,"
            " group_status, deliverable, unlinked_reason)"
            " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (sheet_id, r.row_index, r.bottle_no, r.sample_no, r.lab_salinity,
             raw["id"], raw["row_index"], raw["depth"],
             raw["temperature"], raw["salinity"],
             1 if is_upcast else 0,
             (f"{ex['reason']}（{ex['detail']}）" if is_upcast else None),
             len(groups[r.sample_no]), spread[r.sample_no],
             "pending" if group_pending else "ok",
             1 if deliverable else 0),
        )
        if deliverable:
            delivered += 1
        else:
            pending_cnt += 1

    conn.execute(
        "UPDATE handover_sheets SET delivered_count=?, pending_count=?, unlinked_count=?"
        " WHERE id=?",
        (delivered, pending_cnt, unlinked_cnt, sheet_id),
    )
    return {
        "id": sheet_id,
        "delivered_count": delivered,
        "pending_count": pending_cnt,
        "unlinked_count": unlinked_cnt,
    }


# ---------------------------------------------------------------- 查询

def list_bottle_batches(conn: sqlite3.Connection, cast_id: str) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM casts WHERE cast_id=?", (cast_id,)).fetchone() is None:
        raise _http(404, f"投放 {cast_id} 不存在")
    rows = conn.execute(
        "SELECT * FROM bottle_batches WHERE cast_id=? ORDER BY batch_no", (cast_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_handover_sheets(conn: sqlite3.Connection, cast_id: str) -> list[dict[str, Any]]:
    if conn.execute("SELECT 1 FROM casts WHERE cast_id=?", (cast_id,)).fetchone() is None:
        raise _http(404, f"投放 {cast_id} 不存在")
    rows = conn.execute(
        """
        SELECT h.*, b.batch_no, v.version_no AS raw_version_no,
               b.file_sha256 AS bottle_sha256, v.file_sha256 AS raw_sha256
        FROM handover_sheets h
        JOIN bottle_batches b ON b.id = h.batch_id
        JOIN raw_versions v ON v.id = h.raw_version_id
        WHERE h.cast_id=?
        ORDER BY h.id
        """,
        (cast_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_current"] = bool(d["is_current"])
        result.append(d)
    return result


def get_handover_detail(conn: sqlite3.Connection, sheet_id: int) -> dict[str, Any]:
    head = conn.execute(
        """
        SELECT h.*, b.batch_no, b.filename AS bottle_filename,
               b.file_sha256 AS bottle_sha256, v.version_no AS raw_version_no,
               v.filename AS raw_filename, v.file_sha256 AS raw_sha256,
               v.published_at
        FROM handover_sheets h
        JOIN bottle_batches b ON b.id = h.batch_id
        JOIN raw_versions v ON v.id = h.raw_version_id
        WHERE h.id=?
        """,
        (sheet_id,),
    ).fetchone()
    if head is None:
        raise _http(404, f"交接单 {sheet_id} 不存在")
    d = dict(head)
    d["is_current"] = bool(d["is_current"])
    bool_fields = ("linked", "is_upcast", "deliverable")
    d["rows"] = []
    for r in conn.execute(
        "SELECT * FROM handover_rows WHERE sheet_id=? ORDER BY row_index",
        (sheet_id,),
    ):
        row = dict(r)
        for f in bool_fields:
            row[f] = bool(row[f])
        d["rows"].append(row)
    return d


def get_current_handover_detail(
    conn: sqlite3.Connection, cast_id: str
) -> Optional[dict[str, Any]]:
    cast = conn.execute(
        "SELECT current_version_id FROM casts WHERE cast_id=?", (cast_id,)
    ).fetchone()
    if cast is None:
        raise _http(404, f"投放 {cast_id} 不存在")
    row = conn.execute(
        "SELECT id FROM handover_sheets WHERE cast_id=? AND is_current=1", (cast_id,)
    ).fetchone()
    if row is None:
        return None
    return get_handover_detail(conn, row["id"])


# ---------------------------------------------------------------- 导出

def export_handover_csv(conn: sqlite3.Connection, sheet_id: int) -> tuple[str, str]:
    """导出指定交接单（历史单也可导出依据，现行状态在 META 行标注）。"""
    d = get_handover_detail(conn, sheet_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "记录类型", "投放号", "交接单id", "是否现行", "停用原因",
        "瓶样批次号", "瓶样文件sha256", "原始版本号", "原始版本id", "原始文件sha256",
        "本批容差", "瓶号", "击发采样序号", "实验室盐度",
        "关联状态", "无法关联原因", "原始行号", "原始深度", "原始水温", "原始盐度",
        "是否回升段采瓶", "回升排除说明",
        "同序号平行瓶数", "组内盐度极差", "平行组状态", "是否可交付", "生成时间(UTC)",
    ])
    w.writerow([
        "META", d["cast_id"], d["id"],
        "现行" if d["is_current"] else "已停止现行下载",
        d["invalidate_reason"] or "",
        f"b{d['batch_no']}", d["bottle_sha256"],
        f"v{d['raw_version_no']}", d["raw_version_id"], d["raw_sha256"],
        _fmt(d["tolerance"]), "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
        d["created_at"],
    ])
    for r in d["rows"]:
        w.writerow([
            "待核" if r["group_status"] == "pending" else ("可交付" if r["deliverable"] else "无法关联"),
            d["cast_id"], d["id"],
            "现行" if d["is_current"] else "已停止现行下载",
            d["invalidate_reason"] or "",
            f"b{d['batch_no']}", d["bottle_sha256"],
            f"v{d['raw_version_no']}", d["raw_version_id"], d["raw_sha256"],
            _fmt(d["tolerance"]),
            r["bottle_no"], r["sample_no"], _fmt(r["lab_salinity"]),
            "已关联" if r["linked"] else "无法关联",
            r["unlinked_reason"] or "",
            r["raw_row_index"] if r["raw_row_index"] is not None else "",
            _fmt(r["depth"]), _fmt(r["ctd_temperature"]), _fmt(r["ctd_salinity"]),
            "是" if r["is_upcast"] else "否",
            r["upcast_reason"] or "",
            r["parallel_size"], _fmt(r["salinity_spread"]),
            "待核" if r["group_status"] == "pending" else "合格",
            "是" if r["deliverable"] else "否",
            d["created_at"],
        ])
    fname = (
        f"handover_{d['cast_id']}_b{d['batch_no']}_v{d['raw_version_no']}"
        f"{'_current' if d['is_current'] else '_archived'}.csv"
    )
    return fname, buf.getvalue()


def export_current_handover_csv(
    conn: sqlite3.Connection, cast_id: str
) -> tuple[str, str]:
    """现行交接单下载：无现行单时 409（旧单留存但不提供现行下载）。"""
    detail = get_current_handover_detail(conn, cast_id)
    if detail is None:
        # 区分两种无现行单的原因，便于页面提示
        cast = conn.execute(
            "SELECT current_version_id FROM casts WHERE cast_id=?", (cast_id,)
        ).fetchone()
        if cast is None:
            raise _http(404, f"投放 {cast_id} 不存在")
        if cast["current_version_id"] is None:
            raise _http(
                409,
                f"投放 {cast_id} 的原始版重传后尚未确认发布，"
                "旧交接单已停止现行下载，新版发布后请重新生成",
            )
        latest_batch = conn.execute(
            "SELECT id FROM bottle_batches WHERE cast_id=? ORDER BY batch_no DESC LIMIT 1",
            (cast_id,),
        ).fetchone()
        if latest_batch is None:
            raise _http(409, f"投放 {cast_id} 尚未上传瓶样 CSV，无交接单可下载")
        raise _http(
            409,
            "现行交接单缺失：原始新版发布后尚未重新生成交接单，请先点击“重新生成交接单”",
        )
    return export_handover_csv(conn, detail["id"])


# ---------------------------------------------------------------- 工具

def _counts(sheet: Optional[dict[str, Any]]) -> dict[str, int]:
    if sheet is None:
        return {"delivered_count": 0, "pending_count": 0, "unlinked_count": 0}
    return {
        "delivered_count": sheet["delivered_count"],
        "pending_count": sheet["pending_count"],
        "unlinked_count": sheet["unlinked_count"],
    }
