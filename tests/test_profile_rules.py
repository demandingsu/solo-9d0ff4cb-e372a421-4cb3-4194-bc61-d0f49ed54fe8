"""上行/回升过滤、整米段聚合、缺测、坏行、同序号冲突。"""


def test_upcast_rows_excluded_with_reasons(client, helpers):
    # 深度序列：0.5 -> 5.2 -> 4.8(回升) -> 6.0 -> 5.2(回升) -> 10.0
    rows = [
        ("C1", 1, 0.5, 20.1, 34.1),
        ("C1", 2, 5.2, 18.0, 34.5),
        ("C1", 3, 4.8, 18.4, 34.4),   # 回升
        ("C1", 4, 6.0, 17.5, 34.6),
        ("C1", 5, 5.2, 18.0, 34.5),   # 回升（且与旧最大值相等的重复深度）
        ("C1", 6, 10.0, 15.0, 35.0),
    ]
    r = helpers.upload(client, rows)
    assert r.status_code == 200, r.text
    vid = r.json()["version_id"]
    assert r.json()["retained_count"] == 4
    assert r.json()["excluded_count"] == 2

    d = client.get(f"/api/versions/{vid}").json()
    excluded = d["excluded_rows"]
    assert {e["row_index"] for e in excluded} == {3, 5}
    assert all(e["reason_code"] == "upcast" for e in excluded)
    detail_5 = next(e for e in excluded if e["row_index"] == 5)
    assert "回升" in detail_5["reason"] and "5.2" in detail_5["detail"]
    # 保留行深度严格递增
    retained_depths = [x["depth"] for x in d["retained_rows"]]
    assert retained_depths == [0.5, 5.2, 6.0, 10.0]


def test_equal_depth_is_repeated_and_excluded(client, helpers):
    rows = [("C2", 1, 3.0, 20, 34), ("C2", 2, 3.0, 20, 34), ("C2", 3, 3.5, 19, 34.2)]
    r = helpers.upload(client, rows)
    assert r.status_code == 200
    d = client.get(f"/api/versions/{r.json()['version_id']}").json()
    assert [x["row_index"] for x in d["retained_rows"]] == [1, 3]
    assert d["excluded_rows"][0]["detail"].find("3.0") >= 0


def test_bin_means_counts_and_missing_bins(client, helpers):
    # bin0: 0.5; bin1: 1.2,1.8; bin2 无保留行(缺测); bin3: 3.1
    rows = [
        ("C3", 1, 0.5, 20.0, 34.0),
        ("C3", 2, 1.2, 19.0, 34.2),
        ("C3", 3, 1.8, 17.0, 34.4),
        ("C3", 4, 3.1, 16.0, 34.6),
    ]
    r = helpers.upload(client, rows)
    d = client.get(f"/api/versions/{r.json()['version_id']}").json()
    bins = {b["depth_bin"]: b for b in d["bins"]}
    assert sorted(bins) == [0, 1, 2, 3]
    assert bins[1]["row_count"] == 2
    assert bins[1]["temp_mean"] == 18.0
    assert bins[1]["salinity_mean"] == 34.3
    assert d["missing_bins"] == [2]
    assert bins[2]["row_count"] == 0 and bins[2]["all_missing"]


def test_missing_measurements_count_as_null_not_bad_row(client, helpers):
    rows = [
        ("C4", 1, 0.5, "", 34.0),
        ("C4", 2, 1.5, 18.0, ""),
        ("C4", 3, 2.5, "", ""),   # 段内温盐全缺测 -> 缺测段
    ]
    r = helpers.upload(client, rows)
    assert r.status_code == 200
    d = client.get(f"/api/versions/{r.json()['version_id']}").json()
    bins = {b["depth_bin"]: b for b in d["bins"]}
    assert bins[0]["temp_count"] == 0 and bins[0]["temp_mean"] is None
    assert bins[0]["salinity_count"] == 1
    assert bins[1]["salinity_mean"] is None
    assert 2 in d["missing_bins"]


def test_bad_row_rejects_entire_batch_and_leaves_no_data(client, helpers):
    rows = [
        ("C5", 1, 1.0, 20, 34),
        ("C5", "x", 2.0, 19, 34.2),      # 序号非整数
        ("C5", 3, "abc", 18, 34.3),      # 深度非数字
        ("C5", 4, -1.0, 18, 34.3),       # 负深度
        ("C5", 5, 2.0, "nan", 34.3),     # 非有限水温
    ]
    r = helpers.upload(client, rows)
    assert r.status_code == 422
    errs = r.json()["errors"]
    assert len(errs) == 4  # 所有坏行一次性报告
    # 库内不留任何投放
    assert client.get("/api/casts").json() == []


def test_duplicate_sample_no_with_conflicting_values_rejected(client, helpers):
    rows = [
        ("C6", 1, 1.0, 20, 34),
        ("C6", 2, 2.0, 19, 34.2),
        ("C6", 2, 2.5, 18, 34.1),  # 同序号异值
    ]
    r = helpers.upload(client, rows)
    assert r.status_code == 422
    assert any("异值" in e for e in r.json()["errors"])
    assert client.get("/api/casts").json() == []


def test_duplicate_sample_no_identical_also_rejected(client, helpers):
    rows = [("C7", 1, 1.0, 20, 34), ("C7", 1, 1.0, 20, 34)]
    r = helpers.upload(client, rows)
    assert r.status_code == 422
    assert any("重复" in e for e in r.json()["errors"])


def test_missing_column_and_wrong_field_count_rejected(client, helpers):
    bad_header = "投放号,采样序号,深度,水温\n"  # 缺盐度列
    r = client.post("/api/uploads", files={"file": ("a.csv",
        helpers.csv_text([("C8", 1, 1, 20)], header=bad_header).encode(), "text/csv")})
    assert r.status_code == 422 and "盐度" in r.json()["errors"][0]

    text = helpers.HEADER + "C9,1,1.0,20,34\nC9,2,2.0,19\n"  # 字段数不符
    r2 = client.post("/api/uploads", files={"file": ("a.csv", text.encode(), "text/csv")})
    assert r2.status_code == 422 and any("字段数" in e for e in r2.json()["errors"])


def test_multiple_casts_in_one_file_rejected(client, helpers):
    rows = [("C9", 1, 1.0, 20, 34), ("C10", 2, 2.0, 19, 34.2)]
    r = helpers.upload(client, rows)
    assert r.status_code == 422
    assert any("一个投放" in e for e in r.json()["errors"])


def test_english_headers_accepted(client):
    header = "cast_id,sample_no,depth,temperature,salinity\n"
    text = header + "C11,1,1.0,20,34\nC11,2,2.0,19,34.2\n"
    r = client.post("/api/uploads", files={"file": ("a.csv", text.encode(), "text/csv")})
    assert r.status_code == 200
    assert r.json()["cast_id"] == "C11"
