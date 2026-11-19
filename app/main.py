"""FastAPI 路由与应用入口。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from . import services
from .config import MAX_UPLOAD_BYTES
from .db import get_conn, init_db
from .parsing import BatchRejected
from .services import StalePreview


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="CTD 下行剖面管理", version="1.0.0", lifespan=lifespan)


@app.exception_handler(BatchRejected)
def _batch_rejected_handler(_request, exc: BatchRejected):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=422,
        content={"message": str(exc), "errors": exc.errors},
    )


@app.exception_handler(StalePreview)
def _stale_handler(_request, exc: StalePreview):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"message": str(exc)})


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/uploads")
async def upload(
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"message": f"文件超过 {MAX_UPLOAD_BYTES} 字节上限"},
        )
    return services.upload_raw(conn, raw, file.filename or "unnamed.csv")


@app.get("/api/casts")
def casts(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return services.list_casts(conn)


@app.get("/api/casts/{cast_id}/versions")
def versions(cast_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    return services.list_versions(conn, cast_id)


@app.get("/api/versions/{version_id}")
def version_detail(version_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return services.get_version_detail(conn, version_id)


@app.post("/api/versions/{version_id}/publish")
def publish(version_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return services.publish_version(conn, version_id)


@app.get("/api/casts/{cast_id}/profile.csv")
def profile_csv(cast_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    fname, text = services.export_profile_csv(conn, cast_id)
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/versions/{version_id}/raw.csv")
def raw_csv(version_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    fname, text = services.export_raw_csv(conn, version_id)
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
