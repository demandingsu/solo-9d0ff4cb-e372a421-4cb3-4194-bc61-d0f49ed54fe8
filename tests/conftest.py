import sys
from pathlib import Path

import pytest

# 让测试可以直接从仓库根目录导入 app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试一个隔离的 SQLite 数据目录（通过 monkeypatch 切换 DB_PATH）。"""
    from app import config, db, main

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "ctd.db")
    monkeypatch.setattr(db, "DB_PATH", config.DB_PATH)
    db.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


HEADER = "投放号,采样序号,深度,水温,盐度\n"


def csv_text(rows, header=HEADER):
    return header + "".join(",".join(str(x) for x in r) + "\n" for r in rows)


def upload(client, rows, filename="cast.csv", header=HEADER):
    return client.post(
        "/api/uploads",
        files={"file": (filename, csv_text(rows, header).encode("utf-8"), "text/csv")},
    )


@pytest.fixture()
def helpers():
    return type("H", (), {"csv_text": staticmethod(csv_text), "upload": staticmethod(upload),
                          "HEADER": HEADER})()
