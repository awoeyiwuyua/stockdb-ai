# 上游源探测记录（2026-10-25 部署验收窗口）

- **eastmoney push2his**（push2his/92./21./40. × http/https × cookie/Referer × IPv4/IPv6 pin）：全程不可达（RST/超时；根路径个别节点 404 正常但 API 路径全拒；quote.eastmoney.com 页面域可用）。→ 7 BK 序列 BLOCKED。
- **腾讯 ifzq 日K**：可达性随时间/网络位置漂移——
  04:1x web. 可用（容器+开发机）→ 04:3x web. 501、proxy. 仅开发机 →
  04:4x 无前缀主机仅 NAS 容器可用 → 05:0x 三主机全 501（waf.tencent.com 501page，开发机与 NAS 同时被封，疑高频探测触发）。
  → 引入新浪 getKLineData 第四提供者（commit 5a3757e），最终构建指数组由新浪续供。
- **新浪 getKLineData**：全程可用（I08 外部对照 + 第四提供者）。
- **引擎（本地 7899）与镜像同步**：全程正常（最新交易日 20261023）。
- **pybao SDK 远程**（8.138.149.215:12328）：连接超时（开发机+NAS），get_bars/get_price 不可用（引擎本地数据不受影响）。
