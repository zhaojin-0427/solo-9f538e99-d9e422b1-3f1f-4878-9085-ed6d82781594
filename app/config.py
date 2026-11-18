"""运行配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        # SQLite 数据库文件路径；容器内默认 /data/watershed.db
        self.database_path: str = os.getenv("DATABASE_PATH", "data/watershed.db")
        # 面积容差 = 该系数 × 流域面积，用于吸收浮点噪音
        self.area_tol_rel: float = float(os.getenv("AREA_TOL_REL", "1e-9"))
        # 点查询边界兜底距离 = 该系数 × 流域外接矩形对角线
        self.point_eps_rel: float = float(os.getenv("POINT_EPS_REL", "1e-9"))
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
