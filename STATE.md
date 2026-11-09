# STATE（ns-studio-v2，活文件）

当前：**按任务书 ns2-auto-iterate-brief 迭代**（P0 堵点 → P1 主要项 → P2 前端 → P3 视频收尾）。M0 ✅ · M1 ✅ 11-08 20:18 · M2a/M2b 11-08 22:05

## 任务书迭代进度（证据 reports/p0/ …）
- P0 r1（11-09 01:5x）：B1 ✅ · B2 ✅ · B4 ✅；B3 部分（op_id / cancel）；顺带 M4、M5 部分、M13 部分。证据 reports/p0/r1_B1_B2_B4.md
- P0 r2（11-09 01:55）：B3 ✅（结果落盘 + 重试到接受、409 停；每道并发 1 库强制）。证据 reports/p0/r2_B3.md → **P0 B1–B4 全部关闭**
- P1 r3（11-09 02:0x）：内网拓扑移出仓库树（git 历史里仍有，仓库 PUBLIC → **待项目方定：改私有 / 改写历史**）。证据 reports/p0/r3_netfix.md
- **项目方 11-09 定**：① 仓库保持 PUBLIC、历史不改（项目方接受历史里的拓扑）；② 旧仓库 a12f5890 已 revert（a1c2f6da，旧页恢复原数据源、dist 已重建），
  87c78b47（隧道 18900 / 18893 + /ns2 桥）保留；③ **final 口径 = 队列 reviews 表**（单一来源，不写 review.json）——任务书 / 参考包“final 只认 review.json passed”按此替换
- P1 r4：M2 ✅ 回收线程出错不死、health 报 reaper 状态。证据 reports/p0/r4_M2_reaper.md
- P1 r5：M3 ✅ enqueue / finish / reap 带谓词单条写。证据 reports/p0/r5_M3.md
- P1 r6：M6 ✅ 长镜头改派 gpu36:2（want_lane 列）、前置不过视频行直接 blocked 写原因。证据 reports/p0/r6_M6.md（部署前 queue.db 已备份）
- P1 r7：M8 ✅ enqueue_video 一镜坏不连坐。证据 reports/p0/r7_M8.md
- P1 r8：M9 ✅ 迁移认 review.json（现网 0 个）/ 视频 master_reject；models.lock 两台实测（6 未校验、purelens 重复登记但未安装、两台各 10+ 未登记 LoRA）。证据 reports/p0/r8_M9.md、reports/models_lock_check.md
- P1 r9：M10 ✅ ROLLBACK.md 重写 + ns2/rollback.sh；gpu33 真演练 02:21 通过（数据 seq 167 不变、echo 203 done）。证据 reports/p0/r9_M10.md
- P1 r10：M11 ✅ PLAN 与“QC 不过挡本镜下一步”对齐；gates 看图模块问题报 infra 不冒充 Q9 没过。证据 reports/p0/r10_M11.md
- P1 r11：M5 ✅ 视频提交前 preflight（输入 / LoRA / 首尾帧仍当前 / 尾帧不老于首帧，M7 主体）。证据 reports/p0/r11_M5_M7.md
- P1 r12：M13 ✅ infra 回队退避（30 s 起倍增、上限 10 分钟），人工重派立即可领。证据 reports/p0/r12_M13.md
- P2 r13：前端 ✅ ns2 同源 /ui（总览 15 s 轮询 + 审片页五操作，接口层真机验过；浏览器真点未做——Chrome 扩展未连）。证据 reports/p2/r13_frontend.md
  打开方式：笔记本浏览器 http://127.0.0.1:18900/ （写操作第一次会要密钥：服务器 /root/work/fredw/ns2/secret.key）
- P3 r14：smoke_v2 ep01 全程新系统跑通（script→board→first→last→video→film，无人工派单；墙钟 37 分钟（r14 标题 23 分钟有误，审查 3 更正）；同输入 0 新行：本机证据见 reports/p3/r17，live 待补）；
  **真机抓到首尾帧跨机交接缺失**（视频机用了旧首帧）→ ns2/handoff.py 修好并重跑验证（236 用 v14/v4，film v2）。证据 reports/p3/r14_smoke_full_chain.md
- 待项目方：ep03-S4（成人，首帧待复核 + 触发）（series10 ep01 不重出：项目方 11-09 定 smoke 证明已够）
- P3 r15：总览 need_frames 判定修正、ETA 不计等人的镜。证据 reports/p3/r15_overview_need_frames.md
- 收尾报告：reports/closing_20261109.md（验收逐条 + 10 项遗留）（ep03-S4 首个真活、ep01 整集、同输入 0 新版本）→ minor 余项 → 收尾报告（重试保 created_at 插队）→ minor；然后 P2 前端（ns2 同源新页）、P3

## 线上（gpu33 / gpu36，/root/work/fredw/ns2/）
- API：gpu33 只绑 10.0.0.33:8900（pid 见 api.pid），db data/queue.db（部署前改表都先 .backup：queue.db.bak_r6_* / bak_r12_*）
- worker：gpu33 道 0,1（GPU3 专审图、GPU2 坏卡永不派）、gpu36 道 0,1,2；默认领全六步；结果先落盘 /root/work/fredw/ns2/pending/ 再上报
- 鉴权：写接口 + worker 接口要 X-NS2-Key（密钥 /root/work/fredw/ns2/secret.key，两台同一份，600）；只读 GET 开放；/files 只给图 / 片 / JSON
- worker 只读文件口 :8901（内网、要密钥、支持 Range）；API /files/<host>/… 与跨机首尾帧交接都经它
- 前端：http://127.0.0.1:18900/（笔记本隧道 → gpu33:8900 /ui；总览 15 s 轮询 + 审片页）。旧页（:8800）已恢复原数据源（旧仓库 a1c2f6da）
- 部署：笔记本 scratchpad redeploy.sh（tar ns2 tests frontend → 只停本目录 cwd 的 ns2 进程 → 重起）。切换 / 回滚：`bash ns2/rollback.sh status|to-old|to-new [--dry]`（docs/ROLLBACK.md）
- 旧派单两台全停（STOP 在，笔记本 lifecycle.apply=false）；11-09 02:21 回滚演练后已切回新系统

## M2 结果（PLAN §M2）
- 迁移导入 55 镜 × 3 步（M2a）；film 步 v1（ep01–04 成片在剧本 ±2 秒内，M2a）；唯一总览 + 定稿 / 取消 / 成片 / 成人触发接口（M2b、P0 r1）
- script / board 两步上线并真机跑通（P3 r14：smoke_v2 ep01 全程新系统，无人工派单，墙钟 23 分钟）
- ① ep01 16/16 视频落定：S3 尽力版等项目方点“用这版 / 修改”（系统不代定）

## 坏点队列 / 遗留
- 待项目方：series10 尽力版视频 24 镜定 / 改；ep03-S4 首帧（成人）复核 + 触发
- models.lock：6 个 LoRA sha 未登记、purelens 重复登记（未安装）、两台各 10+ 未登记 LoRA——项目方 11-09 定：暂不强制校验（只读校验照跑、不挡出片），以后再议（reports/models_lock_check.md）
- 仓库保持 PUBLIC（项目方 11-09 定）；git 历史里仍有旧拓扑文件
- tunnel_supervisor.py plan 子命令对现配置 KeyError 'hosts'（旧仓库工具，未修）
- D:\c\… 有 5 个早先误写的文件（Windows python 把 /c/… 当 D:\c\…），非本轮所写，未动

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
