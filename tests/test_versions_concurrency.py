"""整版更正、预览陈旧、追溯、并发确认与交错操作。"""
import threading

import pytest


def _publish(client, vid):
    return client.post(f"/api/versions/{vid}/publish")


def test_full_correction_lifecycle(client, helpers):
    # 第一版：含一个回升行
    v1_rows = [
        ("C1", 1, 1.0, 20, 34),
        ("C1", 2, 5.0, 18, 34.5),
        ("C1", 3, 4.0, 19, 34.2),   # 回升，v1 中被排除
    ]
    r1 = helpers.upload(client, v1_rows)
    v1 = r1.json()["version_id"]
    assert r1.json()["version_no"] == 1

    # 草稿阶段不能下载现行剖面
    dl = client.get("/api/casts/C1/profile.csv")
    assert dl.status_code == 409

    # 发布 v1
    assert _publish(client, v1).status_code == 200
    dl = client.get("/api/casts/C1/profile.csv")
    assert dl.status_code == 200
    text = dl.text
    assert "v1" in text and "排除行" in text and "upcast" in text
    assert "sha256"  # 元信息行存在
    casts = {c["cast_id"]: c for c in client.get("/api/casts").json()}
    assert casts["C1"]["current_version_id"] == v1

    # 第二版更正：修正第 3 行深度为 6.0（早期深度更正改变后续取舍）
    v2_rows = [
        ("C1", 1, 1.0, 20, 34),
        ("C1", 2, 5.0, 18, 34.5),
        ("C1", 3, 6.0, 17, 34.8),   # 现在是有效下潜，不再排除
    ]
    r2 = helpers.upload(client, v2_rows, filename="cast_fixed.csv")
    v2 = r2.json()["version_id"]
    assert r2.json()["version_no"] == 2 and r2.json()["excluded_count"] == 0

    # 旧版立即停止现行下载，旧数据仍可追溯
    assert client.get("/api/casts/C1/profile.csv").status_code == 409
    versions = {v["id"]: v for v in client.get("/api/casts/C1/versions").json()}
    assert versions[v1]["status"] == "superseded"
    assert versions[v2]["status"] == "draft"
    old_detail = client.get(f"/api/versions/{v1}").json()
    assert len(old_detail["excluded_rows"]) == 1  # 旧剖面完整保留

    # 成功重算（确认发布）后恢复供数，且现行剖面来自 v2
    assert _publish(client, v2).status_code == 200
    dl2 = client.get("/api/casts/C1/profile.csv")
    assert dl2.status_code == 200 and "v2" in dl2.text
    # v2 无排除行，因此数据区不应再出现排除记录类型
    data_area = dl2.text.split("META", 1)[1]
    assert "排除行" not in data_area
    detail2 = client.get(f"/api/versions/{v2}").json()
    assert detail2["is_current"] is True
    assert len(detail2["excluded_rows"]) == 0
    assert len(detail2["retained_rows"]) == 3

    # 旧版原始 CSV 仍可下载追溯
    raw = client.get(f"/api/versions/{v1}/raw.csv")
    assert raw.status_code == 200
    assert "投放号,采样序号,深度,水温,盐度" in raw.text
    assert "4.0" in raw.text  # 旧版被更正前的深度仍在


def test_early_depth_correction_changes_later_choices(client, helpers):
    # v1: 第 2 行冲到 8 米，导致第 3、4 行全是回升
    v1 = [
        ("C2", 1, 1.0, 20, 34),
        ("C2", 2, 8.0, 16, 34.9),
        ("C2", 3, 5.0, 17, 34.6),
        ("C2", 4, 7.0, 16.5, 34.8),
        ("C2", 5, 9.0, 15.5, 35.0),
    ]
    r = helpers.upload(client, v1)
    d1 = client.get(f"/api/versions/{r.json()['version_id']}").json()
    assert [x["row_index"] for x in d1["excluded_rows"]] == [3, 4]

    # v2: 更正第 2 行为 3 米 -> 第 3、4 行都变成有效下潜
    v2 = [
        ("C2", 1, 1.0, 20, 34),
        ("C2", 2, 3.0, 19, 34.3),
        ("C2", 3, 5.0, 17, 34.6),
        ("C2", 4, 7.0, 16.5, 34.8),
        ("C2", 5, 9.0, 15.5, 35.0),
    ]
    r2 = helpers.upload(client, v2)
    d2 = client.get(f"/api/versions/{r2.json()['version_id']}").json()
    assert d2["excluded_rows"] == []
    assert [x["depth"] for x in d2["retained_rows"]] == [1.0, 3.0, 5.0, 7.0, 9.0]
    # 剖面段随之更新：保留读数深度 1,3,5,7,9 -> 段 1,3,5,7,9，中间段缺测
    present_bins = {b["depth_bin"] for b in d2["bins"] if b["row_count"] > 0}
    assert present_bins == {1, 3, 5, 7, 9}
    assert d2["missing_bins"] == [2, 4, 6, 8]


def test_stale_preview_cannot_be_confirmed(client, helpers):
    helpers.upload(client, [("C3", 1, 1.0, 20, 34)])
    # 第二个版本（预览了 v1 之后原始版已换）
    r2 = helpers.upload(client, [("C3", 1, 1.0, 20, 34), ("C3", 2, 2.0, 19, 34.2)])
    versions = client.get("/api/casts/C3/versions").json()
    v1 = versions[0]["id"]
    v2 = versions[1]["id"]
    assert r2.json()["version_id"] == v2

    # 确认旧预览 v1 -> 409
    resp = _publish(client, v1)
    assert resp.status_code == 409
    assert "最新原始版" in resp.json()["message"]

    # 旧预览失败不影响新版发布
    assert _publish(client, v2).status_code == 200
    casts = {c["cast_id"]: c for c in client.get("/api/casts").json()}
    assert casts["C3"]["current_version_id"] == v2


def test_rejected_batch_does_not_touch_published_version(client, helpers):
    r = helpers.upload(client, [("C4", 1, 1.0, 20, 34), ("C4", 2, 2.0, 19, 34.2)])
    v1 = r.json()["version_id"]
    _publish(client, v1)

    # 坏批次上传被拒
    bad = helpers.upload(client, [("C4", 1, 1.0, 20, 34), ("C4", "x", 2, 19, 34)])
    assert bad.status_code == 422
    # 现行版仍是 v1，下载不受影响
    assert client.get("/api/casts/C4/profile.csv").status_code == 200
    versions = client.get("/api/casts/C4/versions").json()
    assert len(versions) == 1 and versions[0]["status"] == "published"


def test_identical_reupload_rejected_without_new_version(client, helpers):
    rows = [("C5", 1, 1.0, 20, 34)]
    helpers.upload(client, rows)
    again = helpers.upload(client, rows, filename="copy.csv")
    assert again.status_code == 409
    assert len(client.get("/api/casts/C5/versions").json()) == 1


def test_publish_is_idempotent_and_unknown_version_404(client, helpers):
    r = helpers.upload(client, [("C6", 1, 1.0, 20, 34)])
    vid = r.json()["version_id"]
    assert _publish(client, vid).status_code == 200
    assert _publish(client, vid).status_code == 200  # 幂等
    assert _publish(client, 99999).status_code == 404


def test_concurrent_publish_only_one_wins_and_no_half_state(client, helpers):
    """两个线程同时确认同一草稿版：至多一个真正发布，结果保持一致。"""
    r = helpers.upload(client, [("C7", 1, 1.0, 20, 34), ("C7", 2, 2.0, 19, 34.2)])
    vid = r.json()["version_id"]
    results = []

    def worker():
        results.append(_publish(client, vid).status_code)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 200]  # 幂等：都成功且指向同一版

    cast = next(c for c in client.get("/api/casts").json() if c["cast_id"] == "C7")
    assert cast["current_version_id"] == vid
    detail = client.get(f"/api/versions/{vid}").json()
    # 剖面计数与原始行守恒，没有半版
    total = len(detail["retained_rows"]) + len(detail["excluded_rows"])
    assert total == detail["row_count"] == 2
    assert client.get("/api/casts/C7/profile.csv").status_code == 200


def test_concurrent_correction_and_publish_never_publishes_stale_version(client, helpers):
    """交错：线程 A 准备确认 v1，同时线程 B 上传 v2 整版更正。
    最终要么 v1 已发布后被 v2 取代（无现行版，等 v2 确认），
    要么 v2 先到导致 v1 确认 409——绝不允许 v1 成为错误现行结果。"""
    r1 = helpers.upload(client, [("C8", 1, 1.0, 20, 34)])
    v1 = r1.json()["version_id"]
    barrier = threading.Barrier(2)
    outcomes = []

    def publisher():
        barrier.wait()
        outcomes.append(("publish", _publish(client, v1).status_code))

    def corrector():
        barrier.wait()
        outcomes.append(("upload", helpers.upload(
            client, [("C8", 1, 1.0, 20, 34), ("C8", 2, 2.5, 19, 34.2)]).status_code))

    t1 = threading.Thread(target=publisher)
    t2 = threading.Thread(target=corrector)
    t1.start(); t2.start(); t1.join(); t2.join()

    versions = client.get("/api/casts/C8/versions").json()
    assert len(versions) == 2
    v1_status = next(v for v in versions if v["id"] == v1)["status"]
    cast = next(c for c in client.get("/api/casts").json() if c["cast_id"] == "C8")

    if v1_status == "published":
        # v1 先发布，随后被 v2 整版取代：现行指针必须为空，下载停止
        assert cast["current_version_id"] is None
        assert client.get("/api/casts/C8/profile.csv").status_code == 409
        assert next(v for v in versions if v["version_no"] == 2)["status"] == "draft"
    else:
        # v2 先到：v1 已 superseded，确认旧预览必须失败
        assert v1_status == "superseded"
        assert ("publish", 409) in outcomes
        assert cast["current_version_id"] is None

    # 两种路径下，确认 v2 后都能恢复供数，且整条剖面来自 v2
    v2 = next(v for v in versions if v["version_no"] == 2)["id"]
    assert _publish(client, v2).status_code == 200
    profile = client.get("/api/casts/C8/profile.csv")
    assert profile.status_code == 200 and "v2" in profile.text
    assert "v1" not in profile.text.split("META")[1]  # 数据区不混入 v1 内容


def test_concurrent_uploads_create_distinct_versions_with_consistent_history(client, helpers):
    """并发重传同一投放：版本号不冲突、不重号，历史完整。"""
    helpers.upload(client, [("C9", 1, 1.0, 20, 34)])
    barrier = threading.Barrier(2)
    statuses = []

    def uploader(second_depth):
        barrier.wait()
        r = helpers.upload(client, [("C9", 1, 1.0, 20, 34), ("C9", 2, second_depth, 19, 34.2)])
        statuses.append(r.status_code)

    t1 = threading.Thread(target=uploader, args=(2.0,))
    t2 = threading.Thread(target=uploader, args=(3.0,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert statuses == [200, 200]
    versions = client.get("/api/casts/C9/versions").json()
    assert [v["version_no"] for v in versions] == [1, 2, 3]
    assert len({v["file_sha256"] for v in versions}) == 3
    # 只有最新版是 draft，前两版均 superseded，无现行版直到确认
    assert versions[-1]["status"] == "draft"
    assert all(v["status"] == "superseded" for v in versions[:-1])
    cast = next(c for c in client.get("/api/casts").json() if c["cast_id"] == "C9")
    assert cast["current_version_id"] is None
