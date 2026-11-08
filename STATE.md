# STATE（ns-studio-v2，活文件）

当前：**M1b 视频步**（M0 ✅ 9271eca · M1a ✅ 1c1279d）

## 线上（gpu33 / gpu36，/root/work/fredw/ns2/）
- API：gpu33 10.0.0.33:8900，pid 4015035，db /root/work/fredw/ns2/data/queue.db，日志 logs/api.log
- worker：gpu33 pid 4015683（道 gpu33:0,1；GPU3 专审图）；gpu36 pid 1390048（道 gpu36:0,1,2）。真步函数未上线 → 生产 run 的活一律 blocked，不碰 GPU
- M1a 跨机验收：test-m1a 12/12 done，同镜顺序对、按机分派对（11-08 18:44 JST）
- 旧系统照跑（sv-20261107-13），双写禁令：新 worker 做真渲染前，该机旧派单先停

## M1 剩余
- M1b 视频步：payload = build_jobs 已编译变体（graph + inputs）；worker 上传→提交本机 ComfyUI→等→取片→打分→写回 takes.json。首个真活 ep03-S4（gpu33）
- M1c 首 / 尾帧步：frame_job.build_first / build_last_compose + unified_frame._run
- 验收：一镜全程新系统；同输入 0 新版本；回滚演练

## 已知
- PLAN 修正 ⑦（M1 新）：生产视频真实路径是 build_jobs 变体 + vidgen 提交，不是 h3_video.render
- ep03-S4：旧视频子系统记 waiting_frame，但首尾帧在 takes.json 已过（旧系统多口径的又一例）
