"""全局配置。"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CTD_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "ctd.db"
MAX_UPLOAD_BYTES = int(os.environ.get("CTD_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

# 允许的表头别名（CSV 列名 -> 规范字段）
HEADER_ALIASES = {
    "投放号": "cast_id",
    "cast_id": "cast_id",
    "cast": "cast_id",
    "采样序号": "sample_no",
    "sample_no": "sample_no",
    "sequence": "sample_no",
    "深度": "depth",
    "depth": "depth",
    "水温": "temperature",
    "temperature": "temperature",
    "temp": "temperature",
    "盐度": "salinity",
    "salinity": "salinity",
    "salt": "salinity",
}

REQUIRED_FIELDS = ("cast_id", "sample_no", "depth", "temperature", "salinity")
OPTIONAL_MEASURED = ("temperature", "salinity")  # 允许空值，空即缺测
