# 审片史每日导出（审查 3 §9.5；项目方 11-09 定“加每日导出”）

- `ns2/export_reviews.py`：
  - 以只读方式打开 queue.db，把 `reviews` 和 `triggers` 两张表导出成 `backup/reviews/reviews_<时间>.json`（先写临时文件再改名）。
  - 只留最近 14 份。
  - `--restore <json>` 只补缺的行（`INSERT OR IGNORE`），不覆盖库里已有的定稿 / 驳回。
- `ns2/install_review_export_cron.sh`：
  - 在 gpu33 root crontab 加一行，每天服务器 15:10 EST（= 05:10 JST）跑一次。幂等，重复装只留一行。
  - 装完立刻导出一份。
  - 撤掉的命令写在脚本头。
- 导出文件放在服务器 `/root/work/fredw/ns2/backup/reviews`，**不进公开仓库**。
- 测试：`test_export_reviews_roundtrip_and_keep`（导出内容、只留 N 份、恢复不覆盖）。全量测试 **76 passed**（`export_pytest.txt`）。
- **待 SSH**：在 gpu33 跑 `bash ns2/install_review_export_cron.sh`（项目方已批这一项定时任务），把输出补到本报告。
