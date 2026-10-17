# -*- coding: utf-8 -*-
"""采集服务独立入口（容器化部署用）—— 只挂载采集层，不加载知识面

用法：uvicorn service.app_collect:app --host 0.0.0.0 --port 8187

与 service.main:app（组合入口）的区别：
  * 只挂载 collect_api 的 10 个数据面端点
  * /health 的 neo4j / rules / collect_contract 块为「未加载」提示
    （这些由知识面提供；v3 单独部署时即此形态）
  * 不 import service.knowledge_api → 不引入 Neo4j 依赖

对应容器：gdo/collect（docs/49 §2.1）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                        # noqa: E402

from service.common import (                                       # noqa: E402
    BASE_DIR, _load_env_file, install_internal_call_middleware,
)

_load_env_file(os.path.join(BASE_DIR, ".env"))
_load_env_file(os.path.join(os.path.dirname(BASE_DIR), ".env"))

from service import collect_api                                    # noqa: E402

app = FastAPI(title="GaussDB Ops Collect Service (data plane)", version="6.0.0")
install_internal_call_middleware(app)
app.include_router(collect_api.router)
