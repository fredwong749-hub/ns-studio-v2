# STATE（ns-studio-v2，活文件）

当前：**M2**（M0 ✅ · **M1 ✅ 11-08 20:18 JST**：smoke_v2 ep01-S2 首→尾→视频全走新系统；同输入 0 新行；回滚演练过）

## 线上（gpu33 / gpu36，/root/work/fredw/ns2/）
- API：gpu33 10.0.0.33:8900，pid 4015035，db /root/work/fredw/ns2/data/queue.db，日志 logs/api.log
- worker：gpu33 pid 4015683（道 gpu33:0,1；GPU3 专审图）；gpu36 pid 1390048（道 gpu36:0,1,2）。真步函数未上线 → 生产 run 的活一律 blocked，不碰 GPU
- M1a 跨机验收：test-m1a 12/12 done，同镜顺序对、按机分派对（11-08 18:44 JST）
- **gpu33 旧派单已停**（项目方 11-08 批准）：18:51 JST touch $OFF/STOP + 停 producer 2013138 / night 2013140 / vidgen 2013340 / 回魂的冒烟 producer 3925202（日志 gpu33 /root/work/fredw/ns2/logs/P5_stop_old_gpu33.log）。回魂源 = 笔记本 resident_sync loop → ensure_runs（runs.json producing 的非主剧都拉起；STOP 存在时不起）
- gpu36 旧系统照跑（sv-20261107-13）至 M1 验收
- worker pid（M1b 部署后）：见各机 /root/work/fredw/ns2/worker.pid；data → 旧部署 data 软链（同一账本，双写禁令保证不重叠）
- 首个真活：job 15 ep01-S1 视频试跑（trial：只记 take 不动当前版、新种子），gpu33:1 18:56 起

## M1 剩余
- M1b 视频步：payload = build_jobs 已编译变体（graph + inputs）；worker 上传→提交本机 ComfyUI→等→取片→打分→写回 takes.json。首个真活 ep03-S4（gpu33）
- M1c 首 / 尾帧步：frame_job.build_first / build_last_compose + unified_frame._run
- 验收：一镜全程新系统；同输入 0 新版本；回滚演练

## 已知
- ep03 全 11 镜是成人镜（explicit）：只由项目方触发，新系统不自动渲；gpu33 视频分区当前只有 ep03
- M0 报告更正：首帧 54 done + 1 待复核（ep03-S4 Q9 没过，尽力版）；原报告只看 postflight.ok 偏宽，已统一到 ns2/gates
- PLAN 修正 ⑦（M1 新）：生产视频真实路径是 build_jobs 变体 + vidgen 提交，不是 h3_video.render
- ep03-S4：旧视频子系统记 waiting_frame，但首尾帧在 takes.json 已过（旧系统多口径的又一例）

## M1b 验收证据（reports/m1/）
- job 15 ep01-S1 视频试跑（gpu33:1）：v1 / s2 / s3 三条出片，打分 mean 3.26 / 2.63 / ?，penalty 0.512 / 0.365 / 0.39，全过视频门；trial 不动当前版（takes seq 167 前后一致）
- 首跑 bug：score_clip PATH 无 ffmpeg → 崩被当没过门连渲 3 条（~18 GPU 分钟），188f793 修（打分崩 = infra 不重渲、重派先补打分）
- 同输入两次入队：job 16 created True → False，行数 1，takes 不变
- 回滚演练 19:27–19:29：旧系统起 90 s 无冲突、takes 不变 → 切回新系统，echo job 17 done（rollback_drill.log）

## M1c（9dd2160）
- 首 / 尾帧步 = 旧 FrameExec + Producer 阶梯原代码，经 ns2/legacy_frame.py 子进程在旧部署目录跑（PLAN 原则 5“复用已验证执行层”；M2–M4 萃取进 ns2 后删）
- 新队列管派单 / 计次 / 状态：pass → done（旧 auto_current 设当前）；fail → retry（带上次不过项，FL 触发换构图）；preflight / 成人未触发 → blocked；infra → 回队不计次，连续 5 次 blocked
- 已知限制：infra 回队会重出图（旧系统是只重打分），M2 补“只重打分”
- 验收 run：smoke_v2（独立目录，不碰 series10_v2）。jobs 18–23（S1–S3 首 / 尾帧，gpu33），19:36 起
- gpu33 video_policy.json 手放 smoke_v2 scope（= 笔记本 71271f28，sha 1f12abb4…，备份 .bak_P5_1108），供 build_jobs 编 smoke 视频变体

## M1 验收结论（11-08 20:18 JST，证据 reports/m1/smoke_v2_jobs.jsonl）
- smoke_v2 ep01-S2：首帧 第 2 次过（v11，142 s）→ 尾帧 第 1 次 FL 不过、第 2 次自动换构图档过（v2，62 s）→ 视频 v1 第 1 次过门（penalty 0.075，429 s）；takes active = image 11 / last 2 / video 1（source ns2）
- S1 / S3 首帧：G1“排排站”3 次不过 → best_effort（冒烟剧本构图问题，与旧系统一致）；S1 视频变体 needs_writer（冒烟剧本缺动作描述，A14 坏点）
- gpu33 手放（均 = 笔记本已入库内容，带 .bak_P5_1108）：video_policy.json（71271f28，sha 1f12abb4）、run_style.json（95ddb0f9，sha c459da68：smoke 尾帧 compose，#6）
- 笔记本后台看守被系统内存回收（20:0x，非故障）→ 改用服务器侧轮询，不在笔记本挂常驻

## 旧派单全停（M1 后，PLAN 决策 6A）
- gpu36：20:19 JST STOP + 停 producer 2894126 / night 2894128 / vidgen 2895944 / 回魂冒烟 producer 1273897（gpu36 自 00:16 起三卡全闲；日志 gpu36 /root/work/fredw/ns2/logs/P5_stop_old_gpu36.log）
- **坑（已堵）**：笔记本 resident_sync 的 W18 lifecycle 见旧 producer 心跳过期就 request_restart → resident_restart.sh / start.sh **rm STOP** → 旧派单复活。gpu33 实测 19:05、19:44 两次复活（期间 0 派单 0 出图，旧日志核过，无双写）。
  处置：笔记本 config/pipeline/sop_gate.json lifecycle.apply=false（备份 .bak_P5_1108；旧仓库已封存，此改动只在笔记本工作区，不入旧库）；gpu33 20:22 再停
- 仍在笔记本跑的旧常驻：resident_sync / autoflow / sentinel / scorecard / tunnel_supervisor（各 2 份，非本会话所起，未动）。STOP 与 lifecycle 关之后它们不再能拉起服务器旧派单；M3 后按计划全停
