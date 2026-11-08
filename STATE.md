# STATE（ns-studio-v2，活文件）

当前：**M1c 首 / 尾帧步**（M0 ✅ · M1a ✅ · **M1b 视频步 ✅ 真机验收**：ep01-S1 试跑 3 条全过门、同输入 0 新行、回滚演练通过）

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
