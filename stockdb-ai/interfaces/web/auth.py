"""web.auth — webui token 门禁（0.10.27「从零四件套」之四）。

背景：webui 经节点小宝等隧道出内网时，/api/* 是裸奔的（可读数据、可触发同步/
重启）。本模块提供最低限度的共享密钥门禁：

  - config.WEBUI_TOKEN 为空（默认）→ 门禁关闭，一切放行（纯内网用法零负担）；
  - 设置后：/api/* 与写路径必须带 X-StockDB-Token 请求头（常量时间比较）；
    静态资源（SPA shell/assets）与 /legacy 放行——登录卡片本身要能加载；
  - /mcp 显式豁免（0.10.27 决策，见 docs/design/webui-from-zero.md）：
    Mac 端 MCP 客户端只配了 STOCKDB_HOST，加头会当场断链；MCP 面只读，
    独立鉴权留待 MCP 客户端侧支持 header 后再收。

纯函数无 IO：expected 由调用方传入（handler 读 config.WEBUI_TOKEN），测试零打桩。
"""
from __future__ import annotations

import hmac

# 前端 http.js 同步约定（localStorage 键 webui-token → 请求头）
TOKEN_HEADER = "X-StockDB-Token"


def authorized(path: str, provided: str | None, expected: str) -> bool:
    """path 是否放行。expected 为空 = 门禁关闭恒 True；/api/* 校验请求头。"""
    expected = (expected or "").strip()
    if not expected:
        return True
    if path == "/mcp" or not path.startswith("/api"):
        return True  # 静态/legacy 放行（SPA shell 要先加载）；/mcp 豁免见模块 docstring
    provided = (provided or "").strip()
    return bool(provided) and hmac.compare_digest(provided, expected)
