# 构建身份与回退

- 部署 commit：6fe4803（分支 market-v3-research-extension，基线 0.13.2=b72d445）
- 提交清单：
a97725d fix(acceptance): I08 证据记录实际服务提供者与同源/跨源性质
6fe4803 feat: 指数日K第四提供者（新浪 getKLineData）轮转——腾讯三主机同遭 WAF 封禁的结构性备份；series_catalog 按实际服务提供者如实记录
914e2bc docs: 0.14.0 部署台账
a536af6 fix(acceptance): 源中断时 H06/H07/I06 记录空证据不崩溃
1c998cc fix(acceptance): runner 抗上游中断（指数行空时记录证据不崩溃）
3232a2f docs: 0.14.0 CHANGELOG 条目
5ca56ea fix(acceptance): 基线执行器断言更新到 0.14.0 语义（指数身份解析/板块源错误/裸qfq拒绝/X02/X04 移交扩展执行器）——stub 17 PASS 0 FAIL
aca976b fix(deploy): ifzq 三主机轮转（NAS 容器仅无前缀主机可达）+ nas 模式 latest 注入
ed871e1 feat: 0.14.0——research-extension 1.0.0 服务端落地（A 指数/板块日K + B 因子/R/F/A + 长历史）
- 镜像：stockdb-ai:0.14.0（NAS 本机构建，context=/vol1/stockdb/src，GIT_COMMIT=6fe4803）
- 构建：`git -c core.autocrlf=false -c core.eol=lf archive HEAD` → 上传 → 解包 rsync → `docker compose build && up -d`
  ⚠️ Windows 打包必须禁用 autocrlf（CRLF 破坏 entrypoint shebang，本次实证）。
- 回退：/vol1/stockdb/src-backup/src-before-0.14.0-*.tar.gz + compose.yaml.bak-0.14.0（本地镜像 0.13.2 仍在，改 image 字段即回退）。
- 端点：http://192.168.50.146:8081/mcp（LAN）；引擎 :7899。
