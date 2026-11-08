# ns-studio-v2

NS-Final Studio 重构版（P5，2026-11-08 起）。计划与修正：`docs/PLAN.md`；回滚：`docs/ROLLBACK.md`。

- `ns2/`：新代码（队列 / API / worker / 六步 / 迁移）。
- `services/` `packages/` `tools/` `config/`：旧系统复用件，**选择性闭包**（旧仓库 85f96a1f 的 255 个文件，按原相对路径复制；
  `config/pipeline/runs.json` 是 11-08 03:59 的现网快照，不是原样）。帧步 / 打分实际跑的是服务器旧部署目录里的旧代码（ns2/legacy_*.py 子进程）。

## 跑测试

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt      # Windows：.venv\Scripts\pip
.venv/bin/python -m pytest tests/ -q                                   # 需要本机 ffmpeg 才跑 film 端到端，否则该用例 skip
```
测试用密钥 / 剧本夹具在 `tests/conftest.py`（NS2_KEY=test-key），不碰任何真机。

## 鉴权（B1）
写接口与 worker 接口一律要请求头 `X-NS2-Key`；密钥只在服务器 `/root/work/fredw/ns2/secret.key`（600），不进仓库。
只读 GET（总览 / 队列 / 图片视频）不要密钥；`/files` 只给图 / 片 / JSON。API 只绑 10.0.0.33（内网）。

开发：`PYTHONPATH="services/agent;services/render_router;services/recipes;services/post;packages/schema;packages/qc;."`

M0 迁移校验（只读）：`python -m ns2.migrate --data <旧 data>`；两台服务器各跑一遍后 `python -m ns2.migrate merge ...`，报告在 `reports/`。
