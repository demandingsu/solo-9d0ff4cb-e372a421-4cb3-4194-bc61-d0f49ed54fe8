# CTD 下行剖面管理系统

海洋调查 CTD 投放数据的接收、下行剖面计算、版本化更正与发布下载（Python + FastAPI + SQLite）。

## 业务规则

1. **上传与原始行保留**：CSV 必须含 `投放号,采样序号,深度,水温,盐度` 五列（也接受英文列名
   `cast_id, sample_no, depth, temperature, salinity`）。每个原始行按原始行号完整入库。
2. **整批拒收**：坏行（字段数不符、序号非整数、深度非法/为负、温盐非空但非有限数）或
   同一文件内采样序号重复（含异值冲突）→ 整批拒收（HTTP 422），库内数据不变。
   水温/盐度允许留空，空值按缺测处理。单文件仅限一个投放号。
3. **下行过滤**：按采样序号（原始行序）扫描，仅保留**深度首次严格超过此前最大深度**的读数；
   其余行标记为 `upcast`（回升行；深度相等记为重复深度）并给出排除理由。
4. **整米段剖面**：按 `floor(深度)` 分段给出温盐均值、读数计数与缺测段（覆盖范围内无保留行，
   或段内温盐全缺测）。
5. **整版更正**：同一投放号重传即生成新版本；旧版（无论是否发布过）立即置为 `superseded`
   且**停止现行下载**，旧剖面数据保留可追溯；新版预览并**确认发布**成功后才恢复供数。
6. **预览防陈旧**：只有投放的最新原始版允许发布；预览后若原始版已被替换，确认旧预览返回 409。
7. **并发一致性**：写事务使用 `BEGIN IMMEDIATE` + 条件 UPDATE，整条剖面（原始行、分段、
   排除行、缺测段）来自同一已提交原始版；失败整体回滚，不留半版、不产生错误现行结果。
8. **导出可追溯**：现行剖面 CSV 内含原始版本号、文件 sha256、每个保留读数的原始行号，
   以及全部排除行及理由。

## 启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `ctd-data`（容器内 `/data/ctd.db`）。

## 本地开发 / 无 Docker 环境

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
CTD_DATA_DIR=./data uvicorn app.main:app --reload --port 8000
pytest -q
```

## 页面操作

1. 选择 CSV → “上传并生成预览”；被拒收时页面列出全部坏行/冲突原因。
2. “投放列表”查看版本数与现行状态；点击“查看版本/预览”。
3. 预览整米段均值、缺测段、保留读数与被排除的回升行及理由。
4. 草稿版点“确认发布”；发布后“下载现行剖面”得到带原始版依据的 CSV。
5. 任何历史版本都可下载其原始 CSV 与查看剖面；非现行版本不能作为现行剖面下载。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads` | multipart 上传 CSV，整批校验后生成草稿版与剖面 |
| GET | `/api/casts` | 投放列表（含现行版本） |
| GET | `/api/casts/{cast_id}/versions` | 版本历史 |
| GET | `/api/versions/{id}` | 版本详情：分段、缺测段、排除行、原始行 |
| POST | `/api/versions/{id}/publish` | 确认发布（非最新版返回 409） |
| GET | `/api/casts/{cast_id}/profile.csv` | 现行剖面下载（无现行版返回 409） |
| GET | `/api/versions/{id}/raw.csv` | 指定版本原始 CSV 重建下载 |
