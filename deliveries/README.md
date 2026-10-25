# deliveries — 交付物归档

按日期归档与消费方（stockdb-private 主项目）之间的交接包与回传包。
回传包内的 MANIFEST.sha256.json 覆盖包内全部文件；各 ZIP 的 sha256 见下表。

| 日期 | 文件 | sha256 | 说明 |
|---|---|---|---|
| 2026-10-25 | 2026-10-25-stockdb-research-extension-handoff.zip | da497120338a26fc7769816e2c975baf9f0f540c903a0c27c1462edcec849478 | 消费方输入：research-extension 1.0.0 开发任务包（0.13.2 基线） |
| 2026-10-25 | stockdb-research-extension-return-20261025.zip | 9df7ebb4a55d096def11765610cdc8d01d2fa4bee3dcad471085746526caa523 | 第一轮回传（0.14.0/6fe4803）；gzip-tar 打包缺陷、E01—E08 未修——存档保留 |
| 2026-10-25 | stockdb-research-extension-return-20261025-r2.zip | cc3c069d511a0e721d490f24e2be825f1ba9a7c50e092526fd3d2b5fc5b41c29 | 第二轮回传（0.14.1/55c8e04）：E01—E08 修补 + 真 ZIP + baseline 补齐 |
| 2026-10-25 | stockdb-adjusted-replay-return-20261025-r3.zip | ea3198667b07c8fc3b07c9cce34e06fdacc213478c2ea9673e30090bba82011e | 第三轮回传（0.14.2/30a0bbb）：A 快照重放 meta.adjustment 补修 + 全量证据 |

对应版本/tag：0.13.x（market-v3-core-daily→schema-completion 线，
tag v0.13.0/v0.13.1/v0.13.2）、v0.14.1（55c8e04 线）、v0.14.2（tag 指向
发布线最终 commit）。第三轮反馈输入包
（2026-10-25-stockdb-adjusted-replay-feedback.zip，Downloads）的反例原件
已收编在 r3 包 consumer-feedback-repro/ 内。
