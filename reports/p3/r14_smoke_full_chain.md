# P3 r14：smoke_v2 ep01 整集全程新系统（script → board → first → last → video → film）+ 首尾帧交接 bug

## 跑法
`python -m ns2.enqueue_episode --run smoke_v2 --ep 1 --md data/pipeline/runs/smoke_v2/P4_ep01.md`（gpu33，11-09 02:40 起）。之后全自动，没有人工派单。
job 行：r14_smoke_ep01_jobs.jsonl。

## 首跑踩到并修的（r14a f7b6c30 + 本轮）
1. script 步：旧 import_script 要包路径（ns_agent、v1_leak 闸门的 ns_api）→ 补 PYTHONPATH（215 / 217 / 218 失败，后两条重派成功）
2. 整集开工两台 script 行签名相同 → gpu36 那条被当重复 → 签名带机器
3. **首尾帧交接（严重）**：出图机 gpu33 出了 S2 首帧 v14 / 尾帧 v4，但视频机 gpu36 的 takes.json 还停在旧 v1、没有尾帧，
   视频 226 按 gpu36 本机编译 → 用了旧首帧、不带尾帧（preflight 也只比本机，漏掉）。旧系统靠笔记本同步，新系统缺这一步。
   修：`ns2/handoff.py sync_frames`——视频步编译 / 提交前，若出图机 ≠ 视频机，经出图机文件口拉当前首 / 尾帧文件 + take 到本机、active 指过去；
   本机同号不同文件 → 冲突 blocked 不覆盖；帧变了旧变体作废重编。226 与 film v1 保留为历史（不删）。

## 结果
- 233 film v1（用了错帧的 226）→ 修后重派 236：gpu36 交接到 image 14 / last 4（synced_from gpu33），build_jobs 按 first_frame_take 14、last_frame_take 4 编译，
  v1 第 1 次过门（penalty 0.0，420.6 s），take 2 设当前 → film 237 自动重合 → `data/episodes/smoke_v2/ns2/ep01_v2.mp4`
- S2：board 1.1 s → 首帧第 3 次过（tries 2，115 s）→ 尾帧第 2 次过（145 s）→ 视频 → 成片
- S1 / S3：首帧 G1“排排站”3 次不过 → best_effort 等人（冒烟剧本构图问题，与 M1 一致），其尾帧 / 视频不跑，成片用占位
- 整集用时（overview timing，交接修复前那一轮）：墙钟 23.0 分钟 ≤ 50 分钟预算；GPU 14.9 分钟；逻辑（内容没过重来）17.8 分钟（首帧 8 次重试）；代码 0；检测 3.3 分钟
- **同输入 0 新版本**：同一剧本再跑 enqueue_episode → 217 / 218 existing，行数 26 不变
- 测试 53 passed（r14_pytest.txt）；新增 `test_handoff_pulls_image_host_frames`（真文件口 + 冲突不覆盖 + 重跑无变化）

## 影响面
- series10_v2 ep01–04 的视频都是旧系统出的（当时有同步），不受此 bug 影响；新系统之后在“出图机 ≠ 视频机”的集（ep01/02/04 出图 / 视频分属两台）出视频都走交接
