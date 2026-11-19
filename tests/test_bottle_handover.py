"""瓶样交接单：原始行关联（含回升段）、平行瓶资格、更正、版本停用与交错一致性。"""
from __future__ import annotations

import sqlite3
import threading

import pytest


# 测试用原始投放：序号 5 为回升行（深度 2.1 未超过此前最大 2.5），
# 序号 7 为重复深度行（3.4 = 此前最大 3.4）——两者都必须能被瓶样按原始行关联。
RAW_V1 = [
    ("B1", 1, 0.5, 21.30, 33.80),
    ("B1", 2, 1.2, 20.80, 33.95),
    ("B1", 3, 1.8, 20.10, 34.02),
    ("B1", 4, 2.5, 19.40, 34.20),
    ("B1", 5, 2.1, 19.80, 34.10),   # 回升
    ("B1", 6, 3.4, 18.60, 34.41),
    ("B1", 7, 3.4, 18.62, 34.40),   # 重复深度（回升段）
    ("B1", 8, 5.0, 17.20, 34.66),
]

# 平行瓶：序号 3 两瓶；序号 6 两瓶盐度差 0.10 > 容差 0.05 -> 整组待核；
# 瓶 B-X 采样序号 99 在原始版中不存在 -> 无法关联。
BOTTLES_V1 = [
    ("B1", "B-01", 1, 33.80),
    ("B1", "B-02", 2, 33.96),
    ("B1", "B-03a", 3, 34.02),
    ("B1", "B-03b", 3, 34.04),      # 平行，极差 0.02 <= 0.05 -> 合格
    ("B1", "B-05", 5, 34.12),       # 回升段采瓶
    ("B1", "B-06a", 6, 34.41),
    ("B1", "B-06b", 6, 34.51),      # 平行，极差 0.10 > 0.05 -> 整组待核
    ("B1", "B-07", 7, 34.39),       # 重复深度行采瓶
    ("B1", "B-X", 99, 35.00),       # 原始版无此序号
]


def _publish_raw(helpers, client, rows=RAW_V1):
    r = helpers.upload(client, rows)
    vid = r.json()["version_id"]
    assert client.post(f"/api/versions/{vid}/publish").status_code == 200
    return vid


def _current_sheet(client, cast="B1"):
    sheets = client.get(f"/api/casts/{cast}/handover-sheets").json()
    current = [s for s in sheets if s["is_current"]]
    assert len(current) == 1
    return client.get(f"/api/handover-sheets/{current[0]['id']}").json()


# ---------------------------------------------------------------- 基础规则

def test_handover_links_raw_rows_including_upcast_and_reports_unlinked(client, helpers):
    _publish_raw(helpers, client)
    r = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    assert r.status_code == 200, r.text
    assert r.json()["sheet_created"] is True

    d = _current_sheet(client)
    by_bottle = {row["bottle_no"]: row for row in d["rows"]}

    # 回升段采瓶：取原始行，带原始行号/深度/温盐，且标注回升
    b05 = by_bottle["B-05"]
    assert b05["linked"] is True and b05["is_upcast"] is True
    assert b05["raw_row_index"] == 5 and b05["depth"] == 2.1
    assert b05["ctd_temperature"] == 19.80 and b05["ctd_salinity"] == 34.10
    assert "回升" in b05["upcast_reason"]

    # 重复深度行同样按原始行关联
    b07 = by_bottle["B-07"]
    assert b07["linked"] is True and b07["is_upcast"] is True
    assert b07["depth"] == 3.4 and b07["raw_row_index"] == 7

    # 普通下行行关联正确
    b01 = by_bottle["B-01"]
    assert b01["linked"] is True and b01["is_upcast"] is False
    assert b01["raw_row_index"] == 1 and b01["depth"] == 0.5

    # 无法关联：给出原因且不可交付
    bx = by_bottle["B-X"]
    assert bx["linked"] is False and bx["deliverable"] is False
    assert "99" in bx["unlinked_reason"]

    # 头计数：合格组 1,2,3(2瓶),5,7 共 6 瓶可交付；待核 2 瓶；无法关联 1 瓶
    assert d["delivered_count"] == 6
    assert d["pending_count"] == 2
    assert d["unlinked_count"] == 1

    # 平行组字段
    b03a = by_bottle["B-03a"]
    assert b03a["parallel_size"] == 2
    assert pytest.approx(b03a["salinity_spread"]) == 0.02
    assert b03a["group_status"] == "ok" and b03a["deliverable"] is True


def test_parallel_group_exceeding_tolerance_whole_group_pending(client, helpers):
    _publish_raw(helpers, client)
    r = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    d = _current_sheet(client)
    group6 = [row for row in d["rows"] if row["sample_no"] == 6]
    assert {row["bottle_no"] for row in group6} == {"B-06a", "B-06b"}
    assert all(row["group_status"] == "pending" for row in group6)
    assert all(row["deliverable"] is False for row in group6)
    assert all(pytest.approx(row["salinity_spread"]) == 0.10 for row in group6)
    # 可交付清单（导出的现行 CSV）中不得出现待核瓶
    dl = client.get("/api/casts/B1/handover.csv")
    assert dl.status_code == 200
    lines = [ln for ln in dl.text.splitlines() if "B-06" in ln]
    assert all("待核" in ln and ",否," in ln for ln in lines)


def test_tolerance_boundary_equal_is_acceptable(client, helpers):
    _publish_raw(helpers, client)
    bottles = [
        ("B1", "B-03a", 3, 34.00),
        ("B1", "B-03b", 3, 34.05),   # 极差恰好等于容差 0.05 -> 合格
    ]
    helpers.upload_bottles(client, bottles, tolerance=0.05)
    d = _current_sheet(client)
    assert all(row["group_status"] == "ok" for row in d["rows"])
    assert d["delivered_count"] == 2 and d["pending_count"] == 0


def test_bottle_batch_rejected_entirely_on_bad_rows_and_duplicates(client, helpers):
    _publish_raw(helpers, client)
    bad = [
        ("B1", "B-01", 1, 33.80),
        ("B1", "", 2, 33.96),                 # 瓶号空
        ("B1", "B-03", "x", 34.00),           # 序号非整数
        ("B1", "B-04", 4, "n/a"),             # 盐度非数字
        ("B1", "B-01", 5, 34.10),             # 瓶号重复
        ("B2", "B-06", 6, 34.41),             # 投放号不一致
    ]
    r = helpers.upload_bottles(client, bad, tolerance=0.05)
    assert r.status_code == 422
    errs = r.json()["errors"]
    assert any("瓶号为空" in e for e in errs)
    assert any("整数" in e for e in errs)
    assert any("实验室盐度" in e for e in errs)
    assert any("瓶号 B-01" in e and "重复" in e for e in errs)
    assert any("一个投放" in e for e in errs)
    # 拒收不留批次、不留交接单
    assert client.get("/api/casts/B1/bottle-batches").json() == []
    assert client.get("/api/casts/B1/handover-sheets").json() == []
    assert client.get("/api/casts/B1/handover.csv").status_code == 409


def test_missing_bottle_column_rejected(client, helpers):
    _publish_raw(helpers, client)
    header = "投放号,瓶号,采样序号\n"  # 缺实验室盐度
    text = helpers.bottle_csv_text([("B1", "B-01", 1)], header=header)
    r = client.post("/api/bottle-batches",
                    files={"file": ("b.csv", text.encode(), "text/csv")},
                    data={"tolerance": "0.05"})
    assert r.status_code == 422 and "实验室盐度" in r.json()["errors"][0]


def test_negative_tolerance_rejected(client, helpers):
    _publish_raw(helpers, client)
    r = helpers.upload_bottles(client, [("B1", "B-01", 1, 33.8)], tolerance=-0.01)
    assert r.status_code == 422
    assert client.get("/api/casts/B1/bottle-batches").json() == []


def test_bottle_upload_without_published_raw_creates_no_sheet(client, helpers):
    # 原始版仅草稿、未发布
    r0 = helpers.upload(client, RAW_V1)
    r = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    assert r.status_code == 200 and r.json()["sheet_created"] is False
    # 无现行交接单可下载
    assert client.get("/api/casts/B1/handover.csv").status_code == 409
    # 原始版发布后重新生成：批次不变，依据为该已发布版
    vid = r0.json()["version_id"]
    assert client.post(f"/api/versions/{vid}/publish").status_code == 200
    regen = client.post("/api/casts/B1/handover-sheets/regenerate")
    assert regen.status_code == 200
    d = _current_sheet(client)
    assert d["raw_version_id"] == vid and len(d["rows"]) == len(BOTTLES_V1)
    # 不存在的投放直接 409
    r2 = helpers.upload_bottles(
        client, [("NOPE", "B-01", 1, 33.8)], tolerance=0.05)
    assert r2.status_code == 409


def test_english_bottle_headers_accepted(client, helpers):
    _publish_raw(helpers, client)
    header = "cast_id,bottle_no,sample_no,lab_salinity\n"
    text = helpers.bottle_csv_text([("B1", "E-01", 1, 33.80)], header=header)
    r = client.post("/api/bottle-batches",
                    files={"file": ("b.csv", text.encode(), "text/csv")},
                    data={"tolerance": "0.05"})
    assert r.status_code == 200, r.text
    d = _current_sheet(client)
    assert d["rows"][0]["bottle_no"] == "E-01" and d["rows"][0]["deliverable"] is True


def test_unlinked_parallel_group_keeps_pending_and_never_deliverable(client, helpers):
    _publish_raw(helpers, client)
    # 序号 88 原始版不存在，且平行两瓶盐度差超容差：无法关联 + 整组待核
    bottles = [("B1", "U-a", 88, 34.0), ("B1", "U-b", 88, 34.5)]
    helpers.upload_bottles(client, bottles, tolerance=0.05)
    d = _current_sheet(client)
    assert d["unlinked_count"] == 2 and d["delivered_count"] == 0 and d["pending_count"] == 2
    assert all((not r["linked"]) and r["group_status"] == "pending"
               and not r["deliverable"] for r in d["rows"])


# ---------------------------------------------------------------- 更正与版本停用

def test_correct_one_bottle_resubmits_batch_and_recomputes_group_eligibility(client, helpers):
    _publish_raw(helpers, client)
    helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    first = _current_sheet(client)
    assert first["pending_count"] == 2

    # 整批更正：修正 B-06b（34.51 -> 34.44），序号 6 极差变为 0.03，整组恢复资格
    bottles_v2 = [
        ("B1", "B-01", 1, 33.80),
        ("B1", "B-02", 2, 33.96),
        ("B1", "B-03a", 3, 34.02),
        ("B1", "B-03b", 3, 34.04),
        ("B1", "B-05", 5, 34.12),
        ("B1", "B-06a", 6, 34.41),
        ("B1", "B-06b", 6, 34.44),
        ("B1", "B-07", 7, 34.39),
        ("B1", "B-X", 99, 35.00),
    ]
    r = helpers.upload_bottles(client, bottles_v2, tolerance=0.05,
                               filename="bottles_corrected.csv")
    assert r.status_code == 200 and r.json()["batch_no"] == 2
    new = _current_sheet(client)
    assert new["id"] != first["id"]
    assert new["delivered_count"] == 8 and new["pending_count"] == 0
    assert new["unlinked_count"] == 1
    group6 = [row for row in new["rows"] if row["sample_no"] == 6]
    assert all(row["group_status"] == "ok" and row["deliverable"] for row in group6)

    # 旧批次 superseded；旧交接单留存但停止现行下载
    batches = {b["batch_no"]: b for b in client.get("/api/casts/B1/bottle-batches").json()}
    assert batches[1]["status"] == "superseded" and batches[2]["status"] == "active"
    sheets = client.get("/api/casts/B1/handover-sheets").json()
    old = next(s for s in sheets if s["id"] == first["id"])
    assert old["is_current"] is False and old["invalidate_reason"] == "bottle_correction"
    # 旧单仍可追溯下载，文件名/内容标注 archived
    old_dl = client.get(f"/api/handover-sheets/{first['id']}/handover.csv")
    assert old_dl.status_code == 200 and "已停止现行下载" in old_dl.text
    # 现行下载指向新单
    cur_dl = client.get("/api/casts/B1/handover.csv")
    assert cur_dl.status_code == 200
    assert f"handover_B1_b2_v1_current.csv" in cur_dl.headers["content-disposition"]


def test_raw_reupload_stops_current_handover_until_new_published_and_regenerated(client, helpers):
    vid1 = _publish_raw(helpers, client)
    helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    first = _current_sheet(client)
    assert client.get("/api/casts/B1/handover.csv").status_code == 200

    # 原始版重传（整版更正）：旧交接单立即停止现行下载
    raw_v2 = [r for r in RAW_V1] + [("B1", 9, 6.0, 16.5, 34.80)]
    r2 = helpers.upload(client, raw_v2, filename="cast_v2.csv")
    vid2 = r2.json()["version_id"]
    assert client.get("/api/casts/B1/handover.csv").status_code == 409
    sheets = client.get("/api/casts/B1/handover-sheets").json()
    old = next(s for s in sheets if s["id"] == first["id"])
    assert old["is_current"] is False and old["invalidate_reason"] == "raw_reupload"
    # 旧单留存可查
    assert client.get(f"/api/handover-sheets/{first['id']}").status_code == 200

    # 新版未发布前不能重新生成
    regen = client.post("/api/casts/B1/handover-sheets/regenerate")
    assert regen.status_code == 409 and "发布" in regen.json()["detail"]["message"]

    # 发布新版后重新生成：新单关联新原始版（序号 9 现在可以关联）
    assert client.post(f"/api/versions/{vid2}/publish").status_code == 200
    regen = client.post("/api/casts/B1/handover-sheets/regenerate")
    assert regen.status_code == 200
    new = _current_sheet(client)
    assert new["raw_version_id"] == vid2 and new["id"] != first["id"]
    assert new["batch_id"] == first["batch_id"]  # 仍是同一个已提交瓶样批次
    # 新单依据完整
    assert new["bottle_sha256"] and new["raw_sha256"]
    dl = client.get("/api/casts/B1/handover.csv")
    assert dl.status_code == 200 and "b1" in dl.text and "v2" in dl.text


def test_identical_bottle_file_rejected_without_new_batch(client, helpers):
    _publish_raw(helpers, client)
    r1 = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    assert r1.status_code == 200
    r2 = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05, filename="copy.csv")
    assert r2.status_code == 409
    assert len(client.get("/api/casts/B1/bottle-batches").json()) == 1


def test_regenerate_is_idempotent_and_unknown_sheet_404(client, helpers):
    _publish_raw(helpers, client)
    r = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    sid = r.json()["sheet_id"]
    again = client.post("/api/casts/B1/handover-sheets/regenerate")
    assert again.status_code == 200 and again.json()["id"] == sid
    assert client.get("/api/handover-sheets/99999").status_code == 404


def test_handover_csv_contains_dual_basis_and_upcast_marker(client, helpers):
    _publish_raw(helpers, client)
    helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    text = client.get("/api/casts/B1/handover.csv").text
    assert "瓶样文件sha256" in text and "原始文件sha256" in text
    assert "原始行号" in text
    # 回升段采瓶行带原始行号 5 与“是”标记，且深度是原始 2.1 而非任何整米均值
    b05_line = next(ln for ln in text.splitlines() if "B-05" in ln)
    parts = b05_line.split(",")
    assert "2.1" in parts and parts[parts.index("2.1") - 1] == "5"  # 原始行号 5 后接深度 2.1
    assert "回升段采瓶" in text


# ---------------------------------------------------------------- 交错 / 原子性

def test_concurrent_bottle_uploads_serialize_with_one_current_sheet(client, helpers):
    _publish_raw(helpers, client)
    barrier = threading.Barrier(2)
    statuses = []

    def uploader(sal):
        barrier.wait()
        statuses.append(helpers.upload_bottles(
            client, [("B1", "B-01", 1, sal)], tolerance=0.05).status_code)

    t1 = threading.Thread(target=uploader, args=(33.80,))
    t2 = threading.Thread(target=uploader, args=(33.81,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert statuses == [200, 200]
    batches = client.get("/api/casts/B1/bottle-batches").json()
    assert [b["batch_no"] for b in batches] == [1, 2]
    sheets = client.get("/api/casts/B1/handover-sheets").json()
    current = [s for s in sheets if s["is_current"]]
    assert len(current) == 1                       # 全表至多一张现行单
    cur = current[0]
    assert cur["batch_id"] == batches[-1]["id"]    # 现行单对应最新已提交批次
    # 现行单明细行数与其批次瓶数一致，不存在半张清单
    detail = client.get(f"/api/handover-sheets/{cur['id']}").json()
    assert len(detail["rows"]) == cur["delivered_count"] + cur["pending_count"] + cur["unlinked_count"]


def test_interleaved_bottle_correction_and_raw_reupload_never_half_sheet(client, helpers):
    """交错：瓶样整批更正与原始版重传同时发生。

    最终现行交接单（若存在）必须同时对应同一已提交瓶样批次与同一已发布原始版；
    任一依据变更后旧单立即停用，绝无半张清单或错误现行单。
    """
    _publish_raw(helpers, client)
    helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    barrier = threading.Barrier(2)
    outcomes = []

    def bottle_corrector():
        barrier.wait()
        v2 = [("B1", "B-01", 1, 33.81), ("B1", "B-02", 2, 33.97)]
        outcomes.append(("bottle", helpers.upload_bottles(client, v2, tolerance=0.05).status_code))

    def raw_corrector():
        barrier.wait()
        raw_v2 = [r for r in RAW_V1] + [("B1", 9, 6.0, 16.5, 34.80)]
        outcomes.append(("raw", helpers.upload(client, raw_v2).status_code))

    t1 = threading.Thread(target=bottle_corrector)
    t2 = threading.Thread(target=raw_corrector)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [("bottle", 200), ("raw", 200)]

    cast = next(c for c in client.get("/api/casts").json() if c["cast_id"] == "B1")
    sheets = client.get("/api/casts/B1/handover-sheets").json()
    current = [s for s in sheets if s["is_current"]]

    if cast["current_version_id"] is None:
        # 原始重传后未发布：必须没有现行单，现行下载停止
        assert current == []
        assert client.get("/api/casts/B1/handover.csv").status_code == 409
        # 发布新版后重新生成，恢复一张依据齐全的现行单
        versions = client.get("/api/casts/B1/versions").json()
        latest = versions[-1]
        client.post(f"/api/versions/{latest['id']}/publish")
        regen = client.post("/api/casts/B1/handover-sheets/regenerate")
        assert regen.status_code == 200
        current = [s for s in client.get("/api/casts/B1/handover-sheets").json()
                   if s["is_current"]]
        assert len(current) == 1

    # 无论哪条路径，现行单的两个依据都真实存在且状态匹配
    assert len(current) == 1
    s = current[0]
    detail = client.get(f"/api/handover-sheets/{s['id']}").json()
    batch = next(b for b in client.get("/api/casts/B1/bottle-batches").json()
                 if b["id"] == s["batch_id"])
    version = next(v for v in client.get("/api/casts/B1/versions").json()
                   if v["id"] == s["raw_version_id"])
    assert batch["status"] == "active" and version["status"] == "published"
    assert detail["bottle_sha256"] == batch["file_sha256"]
    assert detail["raw_sha256"] == version["file_sha256"]
    # 明细行守恒：无半张清单
    assert len(detail["rows"]) == batch["row_count"]


def test_write_failure_rolls_back_entire_sheet_no_half_state(client, helpers, monkeypatch):
    """模拟交接单写入中途失败：批次与交接单整体回滚，不留半张清单。"""
    from app import db

    _publish_raw(helpers, client)
    real_connect = db._connect
    failure_on = {"on": False}

    # sqlite3.Connection 是不可变内置类型，无法直接 monkeypatch 方法或改 __class__；
    # 改为替换连接工厂，直接实例化带故障注入的子类（连接参数与真实工厂一致）
    from app import config

    class FlakyConnection(sqlite3.Connection):
        def execute(self, sql, params=()):
            if failure_on["on"] and isinstance(sql, str) and sql.lstrip().startswith(
                "INSERT INTO handover_rows"
            ):
                raise sqlite3.OperationalError("simulated disk I/O error")
            return super().execute(sql, params)

    def factory():
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(
            config.DB_PATH, timeout=30, check_same_thread=False,
            isolation_level=None, factory=FlakyConnection,
        )
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    monkeypatch.setattr(db, "_connect", factory)

    failure_on["on"] = True
    with pytest.raises(Exception):
        helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    failure_on["on"] = False

    # 用独立连接直接查库：批次、交接单、明细全部不存在（事务已回滚）
    conn = real_connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM bottle_batches").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM bottle_rows").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM handover_sheets").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM handover_rows").fetchone()["c"] == 0
    finally:
        conn.close()
    # 服务恢复后可正常提交，生成完整清单
    r = helpers.upload_bottles(client, BOTTLES_V1, tolerance=0.05)
    assert r.status_code == 200 and r.json()["sheet_created"] is True
    d = _current_sheet(client)
    assert len(d["rows"]) == len(BOTTLES_V1)
