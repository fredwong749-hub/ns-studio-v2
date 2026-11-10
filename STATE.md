# STATE（ns-studio-v2，活文件）

**项目方授权（11-10 JST）：出差一个月、全自动、全程授权。** 一次定了三件：① ep06（全集成人）不出，改由 Claude 写一集非成人新剧本代替做 P6 验收；② 换脸 inswapper 本剧非商用、可上生产（默认关 → 一集 A/B → 有效再开）；③ 开关 / 闸门（中景不补鞋、对视不算排排站、视频同样没过就停、Q3 改硬项）有数据支撑就自己开，变差回滚，写进报告。其余红线不变（成人只由项目方触发）。
当前（11-10 R4 质量 6/7）：站位写法 A/B 60 张——文字改不动构图（turbo_previz 由摆位深度图 + 种子定）；摆位图修距离 / 朝向（NS2_PREVIZ_FIT）实测无改善，保持关。**发现双人首帧第 3 次退参考板会画成拼图**（ep05 S1/S7/S11 image3 全是）→ NS2_NO_BOARD_FALLBACK 第 3 次留在造型设定图换种子（reports/p6/r4_staging_ab.md）。ep07 验收剧本已写好（16 镜 93 s、0 成人镜、无三人同框，data_drafts，不进仓库）。
当前（11-10 R4 质量 5）：G1 没过大多是“排排站”几何判；104 张被判图逐张看，yaw 异号相差≥0.5 的 20 张全是对视 / 低头 / 各看一边 → legacy_rules 规则 8 去掉这类对（NS2_LINEUP_FACING 默认关；真 YuNet 重跑只翻这批，reports/p6/r4_lineup_facing.md）。ep05 的 G1 多数是真排排站，根在站位写法（下一项）。
当前（11-10 R4 质量 4）：首帧景别——视频“景别不符”全是首帧带过去的（首帧档差≥2 → 视频 8/9 不符；首帧对 → 0/92）；Q3 早测出但被 L-196 降成只记录。根因之一：中景也被补“鞋必须入画”（中景带鞋句远两档 22% vs 3%，分层成立）→ legacy_rules 规则 7 中景不补看不见的鞋（NS2_MID_NO_FEET 默认关；真编译 68 镜只变 12 镜、只删这一句，reports/p6/r4_mid_no_feet.md）。待项目方：Q3 是否改硬项（L-196 闸门）。
当前（11-10 R4 质量 2b，项目方选“2”）：专门换脸 inswapper + CodeFormer 对焦匹配离线 A/B——ep05 11 张 Q1 没过帧 11/11 过、独立 SFace 0.485→0.707、清晰度 11/11 不降（reports/p6/r4_face_swap_result.md）；**进生产卡在许可证（insightface 非商用）待项目方定**，未接生产。
当前（11-10 R4 质量轮）：香港路线恢复（沿用旧仓库 netfix 常驻隧道）。ep05 质量账（server）：首帧一次过 38.5%、尾帧 0%；首帧没过排行 G1 11 / Q10 8 / Q6 6 / Q1 5，尾帧 G1 15 / FL 14 / Q1 12；视频 25 条过 7，没过 景别不符 8 / 硬切 6 / 泛红 3。**质量 2 只修脸 A/B 不采用**（相似度平均降 0.11、清晰度剩 40%，reports/p6/r4_face_fix_result.md）；**质量 3 视频同样没过就停**（泛红 / 身份 / 硬切，回放 476 条省 10.5% 丢 3 镜，默认关 NS2_SAME_FAIL_STOP，reports/p6/r4_video_same_fail_replay.md）。下一项：首帧加景别检查（景别不符根在首帧）+ G1 看镜头。
当前（11-10 04:0x JST）：**ep05 R4 跑完**——成片 ep05_v1.mp4 67.7 s（7 定稿 / 4 尽力 / 2 占位 S8 S13）；恢复后 175 分钟、首帧一次过 38.5%、尽力版 76.9% → **R4 不达标**（reports/p6/r4_ep05_result.md）。两台工人 04:00 已重启（新编译规则生效），API 未动。
M0 ✅ · M1 ✅ 11-08 20:18 · M2a/M2b 11-08 22:05 · P6 R0 ✅ / R1a ✅ / R3 两台出视频（11-09 项目方“把 gpu36 也用起来”）

## 11-09 晚（P0 live 关闭之后）
- 部署：深圳跳板不通 → 香港路线 gpu33-hk / gpu36-hk（项目 ssh_config，不在本仓库）；部署前 queue.db 备份 bak_brief2_1109
- live 证据：文件口 401 / 403、伪造终态 403、导入口 403、非成人 trigger 400、55 镜定稿时间核实、审片导出 cron（修了 set -e 空表 bug）；ep05 同剧本重入队 0 新行 + takes 字节不变；B3 kill → reap → ComfyUI 中断
- 恢复 ep05 发现并修：尽力版读视图没接到尾帧 preflight / build_jobs（61ae12d）、sop_gate 视频闸门对 ns2 新导入的集永远挡（同上，改由队列前置）、作业表过期不重编（ab… / current(strict)）、ep05-S7 V1 期旧驳回（项目方“清掉”，两台已标 cleared）
- 两台出视频（fafcb91 + 后续）：视频行任一台领、/api/retarget、交接带尽力版 / I2VA 标记、拼片取对方的片、视频机不按旧账复判出图机的验收 / 驳回、认领时本机专属活优先
- 前端按项目方“参看旧 v3 做到 95 分”重做（1383ee5 / 84e5f2f，reports/p7/ui_redesign.md）
- ep05 S8 / S13 视频：剧本 H3 段落不满足 RL-T3（每拍视线 / 表情 / 另一人反应）→ 5 个变体全 lint_error；要写手返修（改剧本 = 新签名，整集重入队，今晚未动）

## 任务书第二版进度（证据 reports/p7/ …，每项一份报告 + pytest txt）
- Pillow 进 requirements（全新 venv 62/62）→ reports/p7/p0_pillow.md
- B1：文件口白名单（queue.db / 密钥 / 日志 403）、live 入队拒伪造终态、迁移走默认关的 /api/import/jobs、密钥多行轮换 + 到期、浏览器 90 天到期 + 两页可清除 → reports/p7/b1_files_import_key.md
- B3：视频 prompt 在 wait 前落盘 + 登记，回收 → 本机心跳取走 /queue 删 + /interrupt，重启撤残留，wait 三个错误分支也撤，回收退避 120 s → reports/p7/b3_reap_interrupt.md（**帧步旧执行器未覆盖**，R2 做）
- 堵点 3：触发先验 is_adult、撤销触发、总览带 adult / triggered、审片页只在成人镜给按钮 → reports/p7/b_adult_trigger.md
- 堵点 4：成片默认不带 force，另设“强制重出成片” → reports/p7/b4_assemble_no_force.md
- 堵点 5：收尾报告假 ✅ 已改 ⚠️；本机证据 reports/p3/r17（live 重跑 + takes seq 对照**待 SSH**）
- promote_best ↔ auto_current 对账（规则 4 + 成人拒绝 + promote_only 接上线）→ reports/p7/promote_best_reconcile.md
- N7 / N8：批量定稿不覆盖已驳回、成人显式拒、撤销定稿按钮 → reports/p7/n7n8_bulk_review.md
- 审片史每日导出（项目方 11-09 批）→ reports/p7/review_export.md（gpu33 定时任务安装**待 SSH**）
- P1 / P2 / P3（本机）：M3 事务 + 谓词、M4 真 HTTP 假 Comfy、M5 preflight fail-closed、M6 锁长道 + 前置失败挡视频、M7 种子账本、M8、M10 set -eu、M13 force 围栏；前端 N1 / N2 / N5 / N6 + 总览口径；P3 提交前复核出图机帧 + handoff N2–N4、0 变体原因、film v1 标注；文档更正
- 收尾：reports/p7/closing_brief2.md（逐项结论、SSH 后要做的事、要项目方定的 4 件：M9 冲突、ep06 整集成人不适合做全自动验收、ep03-S4 / 真点 / gpu36 演练、API 双绑）；全新 venv 92 passed
- SSH 跳板 11-09 约 11:00 JST 起不通 → 20:4x 改走香港路线部署，live 证据已补（见文件顶部）

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
- P3 r14：smoke_v2 ep01 全程新队列跑通（执行层仍是旧部署目录）（script→board→first→last→video→film，无人工派单；墙钟 37 分钟（r14 标题 23 分钟有误，审查 3 更正）；同输入 0 新行：本机证据见 reports/p3/r17，live 待补）；
  **真机抓到首尾帧跨机交接缺失**（视频机用了旧首帧）→ ns2/handoff.py 修好并重跑验证（236 用 v14/v4，film v2）。证据 reports/p3/r14_smoke_full_chain.md
- 待项目方：ep03-S4（成人，首帧 Q9 未过待复核 + 触发；视频行是 blocked，要先定首帧）（series10 ep01 不重出：项目方 11-09 定 smoke 证明已够）
- P3 r15：总览 need_frames 判定修正、ETA 不计等人的镜。证据 reports/p3/r15_overview_need_frames.md
- 收尾报告：reports/closing_20261109.md（验收逐条 + 10 项遗留；“同输入 0 新版本”已从假 ✅ 改为 ⚠️）

## P6 整体重构（项目方 11-09 批，docs/P6_PLAN.md）
- 目标：一整集新剧本全自动、≤ 50 分钟、项目方只点一次“全部通过”（成人触发除外）。阶段 R0 → R1a → R1 → R2 → R3 → R4 → R5 → R6
- 五项决定（P6_PLAN §4）：① 按方案开工；② takes 搬进 gpu33 库（语义不变、旧 takes.json 保留；**R1 未开始**）；③ 基准 ep05、验收 ep06；④ 尽力版一律自动用，**范围 = 全部集（11-09 再确认）**，与 auto_current 对账为规则 4；⑤ 女主角钉死项目方给的一张参考图（服务器 assets/identity/lead_ref_v1.png，不进公开仓库），其他人物全合成
- 新队列步骤：script → assets（一集规划）→ asset（一资产一行，镜只等自己缺的）→ board → first → last → video → film
- R0（a43357a）：ep05 新剧本基线，12/13 镜分镜预检不过（缺前期资产）→ 加 R1a
- R1a（334775b，reports/p6/r1a_ep05_status.md）：资产全自动通，13/13 过分镜；首帧 ready 6 / 尽力版 7、视频 0 条、墙钟 261.6 分钟（远超 50）。系统性原因：参考板印字被照画（Q6）、小脸身份（Q1）、只露手缺人（Q10）、排排站（G1）、尾帧与首帧太像（FL）；gpu33 两道干全集、gpu36 闲（出图分区按集分机，R1 的事）
- R4 先行三处（无 live 证据）：841156c 视频 0 变体带原因（**pick_variant 为 None 的线上路径未覆盖**）、4fa1f95 参考板不印字、042dd21 尾帧全太像退回 I2VA
- resident_sync 覆盖事件（11-09）：笔记本旧常驻用旧清单覆盖服务器 assets_v2/manifest.json（4 条资产登记被冲）→ 经项目方批准停掉旧系统笔记本写服务器的部分，登记已补回（服务器备份 bak_p6_restore_*；恢复记录在旧部署目录，不在本仓库）
- ep06 剧本（16 镜 / 92 秒）已写好（草稿，不进仓库）；**按导入器整集 🔴 = 16/16 成人镜，全部要项目方触发**——做“全自动验收集”要另议
- ep05 停跑到 P0 关完（任务书第二版）→ **11-09 21:26 P0 live 关闭后恢复**

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
- script / board 两步上线并真机跑通（P3 r14：smoke_v2 ep01 全程新队列、执行层仍是旧部署目录，无人工派单，墙钟 37 分钟；r14 写的 23 分钟有误、“GPU 14.9 分钟”无 timing 源文件，待补或删）
- ① ep01 16/16 视频落定（film v1 是用错帧的 job 226 合成的，见 r14；v2 为修正版）

## 坏点队列 / 遗留
- series10 尽力版 24 镜 + 其余：**项目方 11-09 确认是本人在总览页点的“全部通过”**（55 镜 review=passed；具体时间 / 镜数待 SSH 后从 reviews 表核，收尾第 10 项核完再关）
- 待项目方：ep03-S4 首帧（成人）复核 + 触发；浏览器真点总览 / 审片页；gpu36 回滚真演练
- models.lock：6 个 LoRA sha 未登记、purelens 重复登记（未安装）、两台各 10+ 未登记 LoRA——项目方 11-09 定：暂不强制校验（只读校验照跑、不挡出片），以后再议（reports/models_lock_check.md）
- 仓库保持 PUBLIC（项目方 11-09 定）；git 历史里仍有旧拓扑文件
- tunnel_supervisor.py plan 子命令对现配置 KeyError 'hosts'（旧仓库工具，未修）
- D:\c\… 有 5 个早先误写的文件（Windows python 把 /c/… 当 D:\c\…），非本轮所写，未动

## 已知
- ep03 全 11 镜是成人镜（explicit）：只由项目方触发，新系统不自动渲；gpu33 视频分区当前只有 ep03
- M0 报告更正：首帧 54 done + 1 待复核（ep03-S4 Q9 没过，尽力版）；原报告只看 postflight.ok 偏宽，已统一到 ns2/gates
- PLAN 修正 ⑦（M1 新）：生产视频真实路径是 build_jobs 变体 + vidgen 提交，不是 h3_video.render
- ep03-S4：首帧 Q9 未过（尽力版待复核），尾帧 ready；视频 blocked（审查 3 更正：原写“首尾帧已过”不对）

## M1b 验收证据（reports/m1/）
- job 15 ep01-S1 视频试跑（gpu33:1）：v1 / s2 / s3 三条出片、trial 不动当前版（takes seq 167 前后一致）。（打分逐条数字原写“3.26 / 2.63 / ?”无原始证据，审查 3 标未证实，已删）
- 首跑 bug：score_clip PATH 无 ffmpeg → 崩被当没过门连渲 3 条（~18 GPU 分钟），188f793 修（打分崩 = infra 不重渲、重派先补打分）
- 同输入两次入队：job 16 created True → False（只这一行的口头记录；整集同输入证据见 reports/p3/r17，live 待补）
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
- 笔记本旧常驻：11-09 经项目方批准停掉写服务器的部分（计划任务停用，恢复办法：Enable-ScheduledTask，清单在本机 scratchpad old_system_stop_20261109.txt）
