# 复现命令（干净环境）

```bash
# 离线合同自检
uv run python stockdb-ai/interfaces/mcp/contract/research-extension/validate_contract.py --self-test

# 单元/注入测试（合成数据）
cd stockdb-ai && uv run --project .. python -m unittest \
    interfaces.mcp.test_market_v3 interfaces.mcp.test_market_v3_research \
    interfaces.mcp.test_stockdb_mcp_server

# live 取证
cd stockdb-ai/interfaces/mcp && STOCKDB_HOST=192.168.50.146 \
    uv run --project ../../.. python acceptance_v3_research.py --mode live

# 部署端点验收
cd stockdb-ai/interfaces/mcp && STOCKDB_HOST=192.168.50.146 \
    uv run --project ../../.. python acceptance_v3_research.py --mode nas --url http://192.168.50.146:8081/mcp

# 基线回归
cd stockdb-ai/interfaces/mcp && uv run --project ../../.. python acceptance_v3.py --mode stub   # + live/nas 同参数
```
