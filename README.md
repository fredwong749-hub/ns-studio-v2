# ns-studio-v2

NS-Final Studio 重构版（P5，2026-11-08 起）。计划与修正：`docs/PLAN.md`；回滚：`docs/ROLLBACK.md`。

- `ns2/`：新代码（队列 / API / worker / 六步 / 迁移）。
- `services/` `packages/` `tools/` `config/`：旧系统复用件，**按原相对路径原样复制、不改**（M2 后瘦身）。

开发：`PYTHONPATH="services/agent;services/render_router;services/recipes;services/post;packages/schema;packages/qc;."`

M0 迁移校验（只读）：`python -m ns2.migrate --data <旧 data>`；两台服务器各跑一遍后 `python -m ns2.migrate merge ...`，报告在 `reports/`。
