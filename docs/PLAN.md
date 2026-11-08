# NS-Final-Studio 重构计划：废弃旧架构，换极简新架构

- 仓库：`~/workspace/NS-Final-Studio`，commit `85f96a1f`（`85f96a1 线3：撤回 f8749971 误带的 4 个文件`）
- 计划日期：2026-11-08
- 现状：生产卡死；冒烟 3/8（A9–A13 四轮没动，最新 A13 3/8，code 95ddb0f9）；`producer.py` 与 `eval/` 冻结中；Phase R 冻结期
- 目标：废弃旧架构，用最快速度恢复生产。**不是修旧系统，是换一套能跑起来的最小系统。**
- 阅读范围：本计划基于对 commit `85f96a1f` 全部代码的实读（4 个并行阅读分工 + 协调人复核关键点）。`data/` 不在仓库里，所有落盘格式均按代码读写逻辑推断，未与真实文件逐字段核对（见附录未核实清单）。

---

## 0. 先纠正一个前提：A13/A14 的卡死不是优先级让路造成的

任务背景里写“smoke_v2（priority 50）永远让给 series10_v2（priority 0）”，这个描述在当前 commit **不成立**，必须先纠正，否则新架构会去修一个已经不存在的问题：

- `config/pipeline/runs.json`（163 行，实测）当前值：`smoke_v2: priority -1, status producing`；`series10_v2: priority 0, status producing`；`series10_s2: priority 0, status paused`；`series10_v1: priority 0, status archived`。
- `higher_priority_busy()`（`tools/autopilot/producer.py:523-538`）：只数 priority 数字**更小**、且 `status=producing`、且登记表里还有 ready 首/尾帧的其他剧。数字越小越优先。没有任何剧的 priority 比 -1 更小，所以 smoke_v2 在当前 commit **不可能被让路规则挡住**。
- `git log --all -S '"priority": 50'` 在仓库历史中无命中；`f8749971`、`21f425d` 两版 `runs.json` 也都是 -1。STATE.md 坏点 5 写的“priority 50”是裁决 #1（priority 改 -1，commit 354e80c0）执行前的旧描述，没有同步更新。
- A13（`eval/results/20261107-2013.json`）G4 的实际失败原因（实测结果文件）：**超时 1500s（rc=124）+ 尾帧缺 S3 + 视频 3 镜全缺 + 15 个版本被排除**（adopt / 旧窗口 / 文件不存在，例如 S1 image v1 文件不存在）。该次首帧已全出，`priority_yield` 日志未在结果文件中体现。
- A14 的新坏点（STATE.md）：smoke_v2 视频作业全部 `needs_writer`（`SHOT_BLOCKED_NO_VARIANT`），是**视频变体编译层**的问题，与优先级无关（见 §1.3 原因 4）。

结论：**优先级让路机制本身是结构性炸弹（见 §1.3 原因 1），必须扔掉；但 A13/A14 卡死的直接原因是视频编译失败 + 测试与生产抢卡 + 证据口径漂移。**新架构同时解决这两层：删掉让路机制，也不再让测试门与生产共用派单器。

另有一处存疑未核实：STATE.md 称 A9 证据里 smoke producer 在 11-07 22:45:02 只记 `priority_yield→series10_v2`、0 派图。该日志与三版 `runs.json` 均为 -1 的事实对不上（两种可能：日志来自 #1 执行前的窗口，或 `runs.json` 在窗口内被改写）。以代码为准，不以日志为准。

---

## 1. 旧架构全景

### 1.1 规模总览（实测）

| 位置 | 规模 |
|---|---|
| 仓库跟踪文件 | 3,296 个 |
| `tools/` | 499 文件 / 138,602 行 |
| `services/` | 219 文件 / 74,312 行 |
| `apps/web/src` | 223 文件 / 43,053 行 |
| `packages/` | 24 文件 / 5,024 行 |
| `eval/` | 3 文件 / 1,398 行 |
| `tests/` | 434 个 `.py` 文件（约 4,584 行测试函数定义） |

机器：笔记本（Windows + WSL 总调度）+ gpu33（10.0.0.33，3090×4，GPU2 坏）+ gpu36（10.0.0.36）。视频/图片生成走各机器本机 ComfyUI（:8188）。服务器实跑版本 sv-20261107-13（ec89806e，11-07 20:24 部署两台）。

当前生产数据（STATE.md + 11-08 03:45 仪表盘）：4 集 55 镜；首帧 55/55；尾帧 50/55；视频 30/55；通过 0/55。ep01 视频 11/16；ep02 视频 0/16（15 镜尽力版待用户定）；ep03 视频 0/11 排队；ep04 视频 0/12（5 镜尽力版待定）；ep05 剧本在写。

### 1.2 模块清单：它实际在做什么、规模、处置

| 模块 | 实际在做什么（一两句话） | 规模 / 关键文件 | 处置 | 理由 |
|---|---|---|---|---|
| **派单核 `tools/autopilot/producer.py`** | 服务器常驻，每 10 秒一拍：从 takes.json + ledger + leases 推导每镜对象状态（`derive()`），再按优先级、SOP 门、lane 角色、硬上限等十几层过滤派首/尾帧；视频只派生状态，不直接派视频 | 6,415 行；`tick()` 3010–3134，`dispatch_frames()` 3956–4097，`derive()` 1951–2106，`higher_priority_busy()` 523–538 | **扔掉** | 卡死的中心。状态是推导的不是存的；过滤层叠了 15 类跳过原因（D1–D15）+ 5 类整拍返回（A1–A5）；6,415 行单文件不可调试。派单语义萃取后进新队列（见 §1.4） |
| **视频夜班 `tools/series10_video/`** | `build_jobs.py` 预编译视频变体写 `video/jobs.json`；`night.py` 每夜算就绪镜分发到各机；`vidgen.py` 各机跑视频阶梯（变体换 seed，最多 4 次，不过取 penalty 最小标尽力） | `night.py` 1,462 行（`loop_once()` 1350），`vidgen.py` 1,113 行（`run()` 980，`SHOT_BLOCKED_NO_VARIANT` 664），`build_jobs.py` 894 行（`needs_writer` 写入 607–608） | **重写** | 变体预编译 + 阶梯重出是需要的，但现在是独立子系统 + 独立状态文件（`jobs.json` / `state_<host>.json`），与 producer 的登记表各说各话。并进 worker 成为视频步，状态只进 SQLite |
| **API `services/api/ns_api/`** | FastAPI :8800，近百个路由：总览、派单、定稿、SOP、brain、owner queue、producer 门面、jobs、frames-index 等；前端所有数据都经它 | 99 文件 / 37,459 行；`app.py` 2,186 行；`video_index.py` 250 行（`GET /api/videos/overview`） | **重写（只留 8 个接口）** | 路由是旧架构复杂度的镜像：同一事实三处口径（见 §1.3 原因 2）。新 API 只保留 §2.4 的 8 个接口，其余删除 |
| **前端 `apps/web/src`** | 全剧总览页（`projects/Root.tsx` + `v3/pages/SeasonPage.tsx`）、画布审片（`ReviewRow.tsx` 662 行）、视频页、出图页；5 个读接口 + 轮询拼出总览 | 223 文件 / 43,053 行；`SeasonPage.tsx`、`EpisodeKpi.tsx` 228 行、`ReviewRow.tsx` 662 行 | **保留（只改数据源 + 修 2 处口径 bug）** | 前端不是卡死原因。重写前端是另一个项目，会拖垮“最快恢复生产”。只把 5 个读接口收敛成 1 个（§2.4），并修 `ReviewRow.tsx:425`“有首帧就写定稿 #N”和 `status.ts:66-74` 把 `stale_global` 算进卡片过期两个 bug |
| **渲染路由 `services/render_router/ns_render/`** | 出图/出视频的实际执行层：ComfyUI 客户端（check→submit→wait→download）、工作流图现场生成（Qwen/H3）、唯一出图路径 `unified_frame`、takes.json 唯一写入口 `mutate_takes` + 改当前版唯一入口 `set_active` | `pipeline.py` 1,406 行（`mutate_takes()` 424，`set_active()` 593），`unified_frame.py` 514 行（`build_graph()` 35，`_run()` 105），`comfy_client.py` 543 行（`run()` 536），`workflows.py` 292 行，`h3_video.py` 574 行 | **保留核心，扔掉旧路径** | 这是唯一真正“干活”的层，协议和工作流图是对的。扔掉 `pipeline.render_image/render_last_frame/render_video` 旧路径（C5 多轮多候选）与 `score.py` 旧评分，只留 `unified_frame` 一套出图路径 |
| **提示词编译 `services/agent/ns_agent/`** | 确定性提示词编译（触发词最前、角色 DNA 锁、原文透传、风格后缀、成人判定）+ 生成契约 preflight/postflight（Q1–Q9）+ 剧本解析 + 资产引用 | `compiler.py` 930 行（`compile_shot()` 669），`frame_job.py` 1,152 行（`build_first()` 342），`gen_contract/` 约 3,600 行，`asset_refs.py` 2,273 行 | **保留编译器与契约，扔掉外围** | 编译规则是出图一致性的根，`bible_version` 盖章必须留。扔掉 `frame_spec` 的 LLM 可选补全、`vqa/` 与 `packages/qc` 的重叠打分 |
| **视频配方 `services/recipes/ns_recipes/`** | 视频配方引擎：yaml 配方 + provenance + Shot Spec→RenderPlan 编译 + H3 lint；变体 `v1/s2/s3/s4/mot` 的来源 | `compiler.py` 386 行（`_needs_writer()` 379–387），17 个 yaml 配方 | **保留编译器，配方压到最少** | A14 的 `needs_writer` 就出在这里：变体编译失败直接写死状态。保留编译逻辑，但配方 yaml 只留 series10 在用的 3–4 个，其余删；编译失败必须返回缺失项清单，不许只写一个状态词 |
| **SOP 门 `tools/master/sop_gate.py`** | 九阶段门（剧本→导入闸门→资产就绪→提示词→出图→出图检测→尾帧→视频→整集预览）+ 复盘门 + promote 状态机；证据散在 6 类文件里，全部复用现有产物 | 1,334 行；`STAGES` L48，`require()` 738，`require_backoff()` 934，`stage_view()` 1195，`promote()` 1243 | **扔掉** | 九阶段 + 复盘门 + observe/enforce 双模式 + 退避签名，是“证据过期即全集停派”的设计（见 §1.3 原因 5）。压成 4 段前置检查（§2.6 每步的输入契约），不设独立的门服务 |
| **冒烟门 `eval/`** | 8 门验收：G1 启动、G2 输入、G3 角色、G4 生成、G5 合成、G6 规格、G7 幂等、G8 无人值守；G4 要翻 `runs.json` 把 smoke_v2 改 producing 跑 25 分钟窗口 | `smoke.py` 907 行（`gate_timeout_s.G4=1500`），`run_smoke.ps1` 12 行，`test_smoke_harness.py` 434 行；11 个结果文件全部 1–3/8 | **扔掉作为挡生产的门，改写为离线验收脚本** | 冒烟门与生产共用同一派单器、抢同一张卡（见 §1.3 原因 4）。新验收脚本只读 jobs 表 + takes.json，不翻任何全局状态、不占 GPU 卡 |
| **常驻自动流 `tools/factory/autoflow_resident.py`** | 笔记本侧常驻：每 30 秒一拍，推写作（`.done` 的集起子进程）、资产作业、agent 派单、好莱坞打分、看门狗；另有 health_tick、idle_watch 守护线程 | 458 行（`tick()` 120）；同目录 `agent_dispatcher.py` 428 行（默认关）、`api_watchdog.py` 113 行（只记不重启） | **扔掉** | 写作推进、资产作业、打分、巡检四件事塞进一个常驻进程，静默退出难查（曾有 01:16 日志全空退出）。v1 不做自动写作推进；资产与打分并进 worker 步内；巡检由 worker 心跳代替 |
| **同步 `tools/autopilot/resident_sync.py`** | 笔记本↔服务器推代码/数据（tier 1 代码小配置、tier 2 大文件生产数据）、拉回视频、同步 control.json | 3,064 行 | **只留拉回，其他扔掉** | 推代码在新架构里是部署动作，不该由常驻进程做。只保留“拉回成品 + takes.json”的最小逻辑（或直接用 rsync，见 §2.2），其余删除 |
| **隧道 `tools/netfix/`** | 隧道监管（每台服务器一条 ssh 挂全部 `-L` 转发）+ rssh 命令复用（ControlMaster）+ puller 拉回 + partition 分区 | `tunnel_supervisor.py` 964 行；`rssh.py` 273 行；`puller.py` 630 行；`config/netfix/tunnels.json`（18893 不在其中，是手起进程） | **保留（基础设施例外）** | 跳板 sshd MaxStartups 限制 + 握手成本是真实约束，隧道收敛不能扔。18893 要么收进 `tunnels.json`、要么废弃（A14 坏点 ③）。常驻进程口径见 §2.2 说明 |
| **编排器 `services/orchestrator/ns_orch/`** | 11 步状态机（script→frame_spec→first→last→handoff→video→qa→audio→upscale→assemble→accept）+ G2/G3/GPU3/G4/G5 关口 + gpu3 桥（canvas_api :5180）；笔记本只跑其中 4 步 | 1,445 行；`engine.py` 584 行（`EP_STEPS`、`GATES` L42） | **扔掉** | 不是主链路：当前生产走 producer + night + vidgen，编排器只是“看板 + 轻量执行器”。11 步状态机与新 6 步直线重复，关口与 sop_gate 重复 |
| **画布桥 `services/canvas_api/`** | gpu3 侧 FastAPI 渲染桥（:5180），`/api/gpu3/` 下的项目/集/队列接口 | 约 2,400 行；`app.py`、`runs.py`、`progress.py`、`store.py` | **保留现状，不动** | v1 不经过它出视频（视频走 worker 直调本机 ComfyUI）。它只服务 gpu3 侧旧编排器，随编排器一起停用，不删代码、不改接口 |
| **后期 `services/post/ns_post/`** | 成片合成：逐镜拼成片、TTS、校色、响度归一 -14 LUFS、4K upscale、配乐 | `assemble.py` 1,653 行；`tts.py`、`colormatch.py`、`ffmpeg.py`、`music.py` | **保留最小集** | 成片步需要 `assemble.py` + `ffmpeg.py`。TTS / 配乐 / 4K upscale 移到 v1.1（见 §5） |
| **schema 与 QC `packages/`** | Pydantic 模型（ScriptImport、Shot Spec、Take 等）+ 质检门（视频/音频/脸/视线） | `packages/schema/` 14 文件约 2,900 行；`packages/qc/` 10 文件约 1,800 行 | **保留 schema，扔掉 qc** | `Take` 模型就是 takes.json 的结构定义，必须留。`packages/qc` 与 `gen_contract` 的 Q1–Q9 重叠打分，扔掉 |
| **巡检与卡点 `tools/master/watchdog.py`、`report_issue.py`** | 每 30 分钟 10 条巡检规则 + 卡点登记唯一入口（写 `issues.jsonl` + 插 `V2_LIVE.md` 首行） | `watchdog.py` 662 行；`report_issue.py` 137 行 | **扔掉** | 巡检规则进 worker 心跳 + 失败计数；卡点登记改成 jobs 表的 `blocked` 状态 + error 文本，不再维护第二套卡点板 |
| **多剧登记 `tools/autopilot/runs.py` 等** | runs.json 读写、合并登记表、集关闭标记、run 级阶段中心、lane 角色、内容签名 | `runs.py` 369 行，`registry_merged.py` 464 行，`episode_state.py` 480 行，`run_phase.py` 395 行，`lane_roles_producer.py` 759 行，`take_sig.py` 326 行 | **扔掉（只留签名思想）** | v1 只跑一部主剧 + 测试 run，不需要多剧登记表。`take_sig.py` 的签名三段压成幂等键（§2.3 `idem_key`），其余删除 |
| **看图 `tools/vqa/`** | 看图服务客户端（Q9 裁决）+ 多实例管理脚本 `review_instances.sh` | 28 文件 / 4,622 行 | **保留 Q9 调用，扔掉实例管理** | Q9 是 postflight 的一部分，保留调用。`review_instances.sh` 的 scale 管理是 R4 OOM 的直接来源（pid 文件缺失就静默不停），审图实例数固定 1、写死，不再动态 scale |
| **配置 `config/pipeline/`** | runs 登记表、发版开关（`release.json` enabled=false）、SOP 门配置、producer 开关、视频策略等 | `runs.json` 163 行，`release.json` 50 行，`sop_gate.json` 44 行，`producer.json` 23 行；`state.json` 本体不存在（只有 `state.example.json`） | **大幅删减** | 只留三份：`runs.json`（压成 run 清单）、`render_endpoints.json`（道表）、`llm_providers.json`（写作模型）。其余删除；`state.json` 本体缺失本身就是配置漂移的证据 |

### 1.3 为什么旧架构会卡死：5 个结构性原因

下面每条都是结构问题，不是某个 bug。修 bug 不会解决它们，这就是必须废弃而不是继续打补丁的理由。

**原因 1 — 优先级让路是“整拍归零”式阻塞，没有老化、没有配额（`tools/autopilot/producer.py:3956-3966` + `523-538`）**

`dispatch_frames()` 开头调 `higher_priority_busy()`：只要有任何一个 priority 数字更小、状态 producing、登记表里还有 ready 首/尾帧的剧，**整拍直接 return，一单不派**，只记一条 `priority_yield` 日志（5 分钟一条）。它只看“对方有没有 ready 的活”，不看本剧等了多久、不看对方的活是不是永远 ready、不设等待上限、不设配额。只要高优先级剧一直有 ready 帧，低优先级剧就永远饿死。这是一个无老化机制的严格优先级队列，饿死是设计结果，不是故障。更糟的是 priority 存在手写 JSON 里（`config/pipeline/runs.json`），STATE.md 写的 50 与实际的 -1 脱节，说明连“谁优先”这个事实本身都不可信——诊断系统自己就是漂移的。

**原因 2 — 系统没有单一事实来源：状态是推导的，口径有三套（`producer.py:1951-2106`、`services/api/ns_api/video_index.py:111-238`、`episode_kpi.py`）**

登记表 `registry.json` 不是存下来的状态，是 `derive()` 每拍从 takes.json + ledger + leases **重算**出来的；SOP 九阶段的证据散在 6 类文件里（`autoflow_epNN.json`、`_ready_epNN.json`、`epNN_prompts.json`、takes.json、`video/clips/`、`episodes/*.mp4`），任一证据过期或口径漂移就挡住整集。同一事实在三个地方三个说法（实测）：
- “视频已定”：`video_index` 的 `final` 只认 `review.json passed`；`episode_kpi` 的 `video_done` 认登记表 `passed` **或 `best_effort`**；`SeasonPage` 页头只数集摘要里 `video` 字段非空。
- “本机已收到”：`video_index` 对有当前视频的镜不校验文件是否在本机；`frames_index` 校验图是否拉到本机；`EpisodeKpi` 只在 `passed` 且无 url 时补一句“片子还没拉到本机”。
- “首帧已过期”：后端规范（`app.py:435` 自述）说全局编译器升级（`stale_global`）只该在检查器显示，前端 `status.ts:66-74` 却把它算进卡片过期，`ReviewRow.tsx:425` 还在“有首帧就写定稿 #N”。
结果是：排障只能读推导结果，推导结果互相矛盾，STATE.md 坏点 6 的四处口径打架就是症状。没有单一事实来源的系统，修好一个地方会在另一个地方冒出新问题——A9–A13 四轮冒烟 3/8 不动就是这个机制的产出。

**原因 3 — 四层常驻 + 双链路 + 双写入口：任何一层静默退出就停产（`tools/autopilot/producer.py`、`tools/series10_video/night.py`、`tools/series10_video/vidgen.py`、`tools/factory/autoflow_resident.py`、`tools/autopilot/resident_sync.py`）**

生产链路是四层常驻叠起来的：autoflow（写作/资产）→ producer（出图）→ night（视频分发）→ vidgen（各机视频），再加 resident_sync（同步）、tunnel_supervisor（隧道）、api（:8800）、gpu36 看图服务。每层有自己的心跳、锁、pid 文件、接管逻辑；死一个就停，静默退出难查（11-07 01:16 日志全空退出；11-07 视频页全黑 = 视频回拉只剩手动，01:45 自查已报无人修）。更糟的是关键路径有**双实现**：出图有 `pipeline.render_image`（C5 多轮）与 `unified_frame`（K-REN-134 唯一路径）两套；视频有 `build_jobs + vidgen` 与 `h3_video.render / pipeline.render_video` 两套；takes.json 有 `mutate_takes` 唯一写入口，但旧 `_save_takes` 仍在，冲突只记 `takes_conflicts.jsonl` 不阻断。双实现意味着修一处漏一处，账本漂移时无法定位是哪条写路径干的。

**原因 4 — 冒烟门与生产共用同一派单器、抢同一张卡：测试本身就是生产的一个竞争者（`eval/smoke.py` + `tools/series10_video/build_jobs.py:607-608` + `vidgen.py:655-666`）**

G4 的执行方式是：经 `runs.set_status` 把 smoke_v2 翻成 producing，跑 `producer.py once --run smoke_v2 --wait` 25 分钟（`gate_timeout_s.G4=1500`），结束翻回 paused。也就是说，**验证生产的能力，依赖生产的能力**。这个设计有三层死锁：
- 视频编译层对 out-of-scope 的 run 直接 fail-closed：`services/recipes/ns_recipes/compiler.py:379-387` 的 `_needs_writer()` → `build_jobs.py:607-608` 把 `needs_writer` 写进 `video/jobs.json` → `vidgen.py:216-231` 的 `undispatchable_reason()` 非空 → `vidgen.py:655-666` 的 `repair_idle()` 标 `blocked` 并记 `SHOT_BLOCKED_NO_VARIANT`（664 行）。首尾帧过了也无变体可派——A14 的全部视频作业就是这样死的。`needs_writer` 没有 1800 秒宽限（只有纯等帧才有），且只写一个状态词，不告诉人缺什么。
- 测试与生产抢同一张审图卡：smoke resident producer scale 3 对 production scale 1，GPU3 真实 OOM（R4：三审图实例 + 视频 ComfyUI :8191 同卡；R5：两 producer 争审图卡）。测试把生产卡死了，生产卡死又让测试失败。
- G4 的通过条件要求 3 首帧 + 3 尾帧 + 3 视频“本次自己出”（`fresh_take_why`：`take.created_at ≥ t0-2s`、非 adopt、首尾帧要求 `take.producer.lane` 非空），而视频是 VIDEO 工人出的、不经过 producer。测试的判定口径与生产的实际分工不一致，A13 的 15 个版本被排除（adopt / 旧窗口 / 文件不存在）就是这么来的。
一个“证明系统能跑”的门，本身要占用系统最稀缺的资源（GPU 卡 + 派单器 + 25 分钟窗口）才能跑——它不是验收，它是负载。

**原因 5 — 闸门是“证据过期即全集停派”的 fail-closed 设计，没有逃生口（`tools/master/sop_gate.py:738` + `934` + `producer.py:4153`）**

`sop_gate.require()`（enforce 抛 `SopGateError`）经 `producer.sop_gate_block()`（`producer.py:4153`，走 `require_backoff`）在派单前拦截；`import_revalidate` 在 `enforce` 模式下 block>0 的集不能进“出图”及以后；复盘门要求第 N+1 集进“出图”起第 N 集复盘 done；打分积压超 `dynamic_backlog` 上限停派；Q9 的 na/pass 口径三方不一致。任何一个证据文件过期、任何一次复验 block、任何一次打分积压，整集停派，且没有“跳过并记录、继续跑别的镜”的逃生口。fail-closed 本身没错（证据缺失 = 不派是对的），错的是**把 fail-closed 加在整集粒度上**：一镜的证据问题停掉全集，一集的复盘问题停掉下一集。A14 坏点 ②（窗口前 5 分钟 `lane_roles_wait`“出图位已转视频位”+ `review_resident_hold`）就是这种整集级联阻塞的日常形态。

### 1.4 新系统必须继承的语义（从旧代码里萃取，丢了会出生产事故）

这些是旧架构里真正对的、经过生产验证的语义，重写时必须原样保留（只换实现方式）：

1. **takes.json 一镜一账本**：版本 `n`、active 指针、postflight、种子、提示词 sha、compile_trace。`set_active()` 拒坏图 / 已标不要 / 被驳回版的规则保留（`ns_render/pipeline.py:593`）。
2. **版本号只因真实新素材 +1**：`_next_n = 1 + max(同 stage 已有 n)`；同输入重跑不占新版本号（STATE 线 4 验收口径；旧系统补丁 v3 已修未上线，新系统第一天就要做到，用幂等键保证，§2.3）。
3. **内容签名三段**：`sig = {script_sig, prompt_sha, rule_fp, v:1}`（`take_sig.py`）。新系统压成幂等键 `idem_key`（§2.3），不再维护独立的签名检查服务。
4. **3 次上限**：`MAX_TRIES=3`（`producer.py:169`）；infra 错误退避、不计次；3 次用尽取最好一版标 `best_effort`、等人工定（L-196）。
5. **认领防重**：leases（45 分钟过期，O_EXCL）→ 新系统用 SQLite 原子认领 + 心跳超时回收（§2.3）。
6. **种子永不复用**：每镜每 stage 用过的种子不重用（`seed_stream.py`，44 行）。
7. **提交前 preflight**：参考图登记 / 脸槽 / 连续性校验不过不提交（`gen_contract/preflight.py:207`）；尾帧不能老于首帧（`producer.py:417-431` E13）。
8. **打分与派单分离**：postflight Q1–Q8 + Q9 只打分、不拦路（旧系统里打分积压会反压停派，新系统删掉这个反压）。
9. **成人镜只由项目方触发**（L-128）：作业 `adult` 标记 + 派单拦截保留，改成 jobs 表的 `owner_trigger` 字段 + 前端一个按钮。
10. **ComfyUI 提交协议**：check（`/object_info`）→ submit（POST `/prompt`，带 `client_id` + `extra_data.ns_tag`）→ wait（轮询 `/history/{id}`，300 秒宽限、超时不重派）→ download（`/view`，失败重试 3 次、不重渲染）；`[output]` 复用 + 上传去重（上行只有 8–25KB/s 时的关键优化）。（`comfy_client.py:245/428/456/512/536`）
11. **工作流图本身**：Qwen 图（`workflows.qwen_edit/qwen_t2i`）、H3 图（`workflows.h3_fl2va`，17k+5 帧网格、禁加速节点清单、LoRA-shift 配对校验）、首尾帧→视频的 H3 语义（对齐句按帧重算、I2VA 去 Picture 2 句、`lint_h3` block 拦截、长镜头只派 gpu36:2、不截短）。
12. **失败关闭的粒度收窄**：证据缺失 = 不派（保留），但只挡住**那一镜的那一步**，不挡整集、不挡下一集（这是对原因 5 的修正，不是推翻 fail-closed）。

---

## 2. 新架构设计：极简，目标是最快恢复生产

### 2.1 目标与原则

- **唯一目标**：让 ep01–ep04 的视频继续出、成片继续拼。衡量标准沿用项目方已定的 KPI（L-205）：**一集 ≤ 50 分钟，一段视频 ≈ 5 分钟**。
- **原则**：
  1. 状态存在表里，不推导。SQLite 队列表就是唯一的派单事实来源；takes.json 是唯一的产物事实来源。两者各管各的，不互相推导。
  2. 失败只影响一镜的一步，不级联。任何一步失败只把那一行标 `failed`/`blocked`，其他行继续跑。
  3. 测试是普通任务，不占特权。测试跑单独 `run_id`，进同一个 FIFO 老实排队，不翻全局状态、不独占卡。
  4. 常驻进程只有两类：API（笔记本 1 个）+ worker（每台 GPU 机器 1 个）。隧道监管是基础设施例外，见 §2.2。
  5. 复用已验证的执行层（§1.2 保留部分），只重写派单与状态层。不要重写 ComfyUI 协议、提示词编译、工作流图——那些是对的。

### 2.2 组件图

```
                        笔记本（Windows + WSL）
  ┌──────────────────────────────────────────────────────────────────┐
  │  前端 apps/web（原样跑，只改 5 个读接口 → 1 个，见 §2.4）          │
  │        │  GET /api/overview · POST 派单/定稿                     │
  │  ┌─────▼────────────────────────────────────────────────────┐    │
  │  │  ns2_api：FastAPI :8800（唯一常驻进程 ①）                  │    │
  │  │   · 唯一写 SQLite 的进程                                   │    │
  │  │   · 读 takes.json / review.json 算总览                      │    │
  │  └─────┬──────────────────────────────┬─────────────────────┘    │
  │        │ SQLite queue.db（队列表，§2.3） │ data/renders/...        │
  │        │                              │ takes.json（产物账本）   │
  └────────┼──────────────────────────────┼──────────────────────────┘
           │ ssh 隧道（tunnel_supervisor 管理，沿用 tunnels.json）    │
           │  · ComfyUI：18188→gpu33:8188、18288→gpu36:8188 等（已存在）│
           │  · 新增一条 worker-api：18800→笔记本 :8800（worker 调 API 用）│
           │  · 18893 要么收进 tunnels.json、要么废弃（A14 坏点 ③）  │
     ┌─────┴──────────────┐        ┌──────────────┴─────┐
     │  gpu33（10.0.0.33） │        │  gpu36（10.0.0.36） │
     │  ns2_worker ②      │        │  ns2_worker ②      │
     │  · 每 5 秒调 API    │        │  · 每 5 秒调 API    │
     │    /worker/claim 领活│       │    /worker/claim 领活│
     │  · 直调本机 ComfyUI │        │  · 直调本机 ComfyUI │
     │    :8188/:8189/:8191│        │    :8188/:8189/:8190│
     │  · 写本机 takes.json │       │  · 写本机 takes.json │
     │  · 调 API /finish 上报│      │  · 调 API /finish 上报│
     └────────────────────┘        └────────────────────┘

  文件拉回：worker 写完本机产物后，沿用 puller.py 最小逻辑（只拉
  takes.json + 成品文件 + 当前版副本，不拉中间产物），由 API 侧每
  60 秒触发一次；或 v1 直接用 rsync 定时任务。拉回失败不阻塞派单，
  只影响前端“本机已收到”的显示（显示规则见 §2.4 口径表）。
```

**常驻进程口径说明**：业务常驻只有 API + worker 两类（笔记本 1 个 API 进程，gpu33/gpu36 各 1 个 worker 进程，共 3 个进程）。` tunnel_supervisor.py`（964 行）作为基础设施保留——它是 ssh 主连接 + 端口转发的监管，不是业务派单；如果严格只留两个业务进程，它不计入。worker 启动时自检隧道（调 `127.0.0.1:18188/queue` 与 `18800/health`），不通则报错退出、由 systemd 重启；supervisor 本身的 15 秒探测 + 杀掉重起逻辑原样保留，不重写。ComfyUI 各实例由各机器自带方式拉起（现状），不新增监管。

**为什么 worker 不直接写 SQLite**：SQLite 文件在笔记本上，远端机器不能安全地跨网络写它（锁不可靠）。所以 API 是 SQLite 的**唯一写者**；worker 只通过 HTTP 认领（claim）、报心跳（heartbeat）、报完成（finish）。worker 与 API 之间走新增的 18800 隧道转发，不走公网。

### 2.3 队列表 schema（SQLite，`data/queue.db`，笔记本）

```sql
-- 队列表：派单的唯一事实来源。状态只由 API 写，worker 只读 claim 结果。
CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,            -- series10_v2；测试用 test-<yyyymmdd-hhmm>
  ep          INTEGER NOT NULL,         -- 集号
  shot        TEXT NOT NULL,            -- ep01-S1
  step        TEXT NOT NULL,            -- script|board|first|last|video|film
  stage       TEXT,                     -- image|last|video（与 takes.json stage 对齐；script/board/film 为 NULL）
  state       TEXT NOT NULL DEFAULT 'queued',
              -- queued|running|done|failed|blocked|best_effort|na
              -- na：本镜不用这一步（如 L-204 D8 不用尾帧的镜，last 步记 na）
  payload     TEXT NOT NULL DEFAULT '{}', -- JSON：输入路径、提示词、seed、lane、变体、tier
  result      TEXT,                     -- JSON：产物路径、take n、qc 分数、film 缺镜清单
  lane        TEXT,                     -- gpu33:0 / gpu33:1 / gpu33:3 / gpu36:0 / gpu36:1 / gpu36:2 / laptop:0
  host        TEXT,                     -- gpu33 / gpu36 / laptop（认领的机器）
  tries       INTEGER NOT NULL DEFAULT 0,
  max_tries   INTEGER NOT NULL DEFAULT 3,
  idem_key    TEXT UNIQUE,              -- 幂等键：sha16(run/ep/shot/step/输入签名)，见下
  owner_trigger INTEGER NOT NULL DEFAULT 0, -- 1 = 成人镜，须项目方在前端点触发才可领
  created_at  TEXT NOT NULL,            -- ISO
  started_at  TEXT,
  finished_at TEXT,
  error       TEXT                      -- 失败/阻塞原因文本；blocked 时写缺失项清单
);
CREATE INDEX idx_jobs_claim ON jobs(state, run_id, created_at);   -- 认领扫描
CREATE INDEX idx_jobs_shot  ON jobs(run_id, ep, shot, step);      -- 按镜查进度

-- 剧清单：v1 只有两行。series10_s2（paused）、series10_v1（archived）不迁移。
CREATE TABLE runs (
  run_id  TEXT PRIMARY KEY,             -- series10_v2
  title   TEXT NOT NULL,                -- 《透明商战》
  status  TEXT NOT NULL DEFAULT 'producing'  -- producing|paused
);
```

**关键语义**：
- **FIFO，禁止优先级让路**：认领查询是 `SELECT ... WHERE state='queued' AND owner_trigger=0 ORDER BY created_at LIMIT 1`，再加本镜顺序约束（同镜只领当前步：`first` done 之前不领 `last`，`last` done/na 之前不领 `video`）。没有任何 priority 字段，没有任何“高优先级剧整拍让路”的逻辑。测试 run 与生产 run 进同一张表、按 `created_at` 排队。
- **幂等键**：`idem_key = sha16(run_id + ep + shot + step + script_sig + prompt_sha + rule_fp)`，三段签名沿用 `take_sig.py` 的定义（`script_sig` = 剧本 8 字段哈希；`prompt_sha` = 编译提示词哈希；`rule_fp` = 三份配置文件整文件哈希）。重复提交同一输入返回已有 job，不产生新行、不占新版本号。这是 STATE 线 4 验收（同输入连跑两遍 0 新版本文件）在新系统的实现方式。
- **认领是原子的**：`BEGIN IMMEDIATE; UPDATE jobs SET state='running', host=?, started_at=? WHERE id=? AND state='queued'; COMMIT;`——只有一行能抢到。worker 每 30 秒调 `/worker/heartbeat`；45 分钟无心跳，API 把任务打回 `queued`、`tries+1`（沿用旧 `LEASE_S=45min` 的租约时长，`producer.py:174`）。`tries ≥ max_tries` → `failed`，不再自动重试。
- **失败分两级**：`failed` = 执行失败（ComfyUI 不可达、超时、崩溃），可人工重派；`blocked` = 输入缺失（缺参考图、变体编译不过、成人镜未触发），带缺失项清单写在 `error` 里，人工补齐后重派。`blocked` 不计 `tries`。
- **打分不进表**：QC 分数只写进 `jobs.result.qc` 和 takes.json 的 `postflight`/`score` 字段，不另建 qc 表，不设打分积压上限，打分慢不影响派单。

### 2.4 核心 API 清单（FastAPI :8800，只保留 8 个接口）

| 接口 | 方法 | 作用 | 来源 / 说明 |
|---|---|---|---|
| `/api/overview?run=` | GET | **唯一总览只读接口**。合并旧 `/api/season` + `/api/episodes/summary` + `/api/videos/overview` + `/api/episodes/kpi` + `/api/pipeline/status` 五个接口。返回 §2.4 下方 JSON 结构 | 旧实现见 `season.py:240-256`、`video_index.py`、`episode_kpi.py`；合并方案按子报告 §5.1，但口径按下方口径表定死 |
| `/api/jobs?run=&ep=&state=` | GET | 队列只读（调试与排障用）。返回 jobs 表行（脱敏后） | 新增；替代旧 `/api/jobs` 路由组 |
| `/api/shots/{shot_id}/jobs` | POST | 派单。body `{stage: first\|last\|video, mode?: regen\|edit\|refs, seed?, gpu?: auto\|道 id, tier?: preview\|final, by: manual\|auto}`；服务端复用现有前置检查（`material_readiness.job_gate`、首尾帧定稿检查、H3 lint）；`by=auto` 时资料不全 409，手动只告警 | 沿用旧 `frame_jobs.py:269` + `video_jobs.py:201` 的前置语义，合并成一个接口 |
| `/api/shots/{shot_id}/review` | PUT | 定稿 / 驳回。body `{state: passed\|rejected\|unreviewed, reason?}`，写该镜 `review.json`；`passed` 即 `final` 的唯一判定 | 沿用旧 `app.py:1909-1921`；成人镜触发也走这里（`owner_trigger` 由本接口清掉） |
| `/api/shots/{shot_id}/cancel` | POST | 取消排队任务。仅 `state=queued` 可取消；在途任务不取消（等它跑完或超时回收） | 新增；旧 jobs 路由组有 cancel，行号未核实，新实现只认 jobs 表 |
| `/api/episodes/{ep}/assemble` | POST | 触发成片：给该集插一行 `film` 步任务进队列 | 新增；替代旧 `tools/assemble_episode.py` 的手动调用 |
| `/api/worker/claim` | POST | worker 认领。body `{host, lanes[], capacity}`；服务端原子 UPDATE 后返回一条 job 或空。同一 worker 一次只领一条（每卡并发 1，见 §2.7） | 新增 |
| `/api/worker/heartbeat` | POST | worker 心跳 + 在途任务进度。body `{host, job_id?, progress?}`；45 分钟无心跳回收任务 | 新增 |
| `/api/worker/finish` | POST | worker 上报完成 / 失败。body `{job_id, state: done\|failed\|blocked\|best_effort, result?, error?}`；API 写 jobs 表；takes.json 由 worker 在上报前直接写盘 | 新增 |
| `/api/health` | GET | 健康检查：API 活着 + SQLite 可写 + 各 worker 最近心跳时间 | 新增 |

**删除的接口**（v1 不再提供，前端同步下线调用）：`/api/sop/*`、` /api/brain/*`、` /api/owner/queue`、`/api/producer/*`（pause/resume/gate；暂停改为 worker 停领 + runs 表 status=paused）、`/api/frames-index`、`/api/season`、`/api/progress`、`/api/episodes/kpi`、`/api/pipeline/status`、`/api/episodes/{ep}/readiness`（资料检查并进派单接口的前置，不再单独成接口）、`/api/jobs/{id}` 旧路由组、`/api/candidates`（画布候选用 overview 的 shots 内嵌 candidates 数字代替，详情抽屉仍可调旧接口的只读部分——v1 先留着，M4 再决定删不删）。

**前端要改的调用点（共 5 处，小修，不重构前端）**：
1. `SeasonPage.tsx` 的 `useSeason`（`/api/season`，60s）+ `useTotals`（`/api/progress`）+ `EpisodeKpi`（`/api/episodes/kpi`，30s）+ `BrainPanel`（`/api/brain/status`，30s）+ `PipelineMatrix`（`/api/pipeline/status`，30s）→ 全部改调 `GET /api/overview`，轮询统一 15 秒。
2. `VideosPage.tsx` 的 `useOverview`（`/api/videos/overview`，15s）→ `/api/overview` 的 `videos` 小节。
3. `FramesPage.tsx`（`/api/frames-index`，4s/20s）→ `/api/overview` 的 `shots` 内嵌首尾帧状态。
4. 顶栏圆点 `NavTabs.helpers.ts` 的 `useVideosSummary` → `/api/overview` 的 `totals` + `videos`。
5. `ReviewRow.tsx` 的 `setReview`（`PUT /api/shots/{id}/review`）→ 路径不变，后端换成新实现；同时修 `ReviewRow.tsx:425-426`“有首帧就写定稿 #N”（改为只当 `locked_take == 当前版` 才写定稿，判定逻辑沿用 `status.ts:118-140` 的 `frameState`）和 `status.ts:66-74` 把 `stale_global` 算进卡片过期（改为 `stale_global` 只进独立字段、不进 `stale`）。

**总览接口口径表（必须定死，否则重蹈 §1.3 原因 2）**：

| 口径 | 新定义（唯一） |
|---|---|
| `final`（视频已定） | 只认队列 `reviews` 表 passed 且该镜最新视频行有片（项目方 11-09 定：单一来源，不写 / 不读 review.json）。登记表 `passed` 不算 |
| `video_done` | 与 `final` 同义。`best_effort` **不算** done，单独计数 |
| `total` 分母 | 剧本镜数（`script_v2.json` 的镜数），不再是“有 takes.json 的目录 ∪ 登记表” |
| `best_effort` | 独立状态，有筛选项、有计数，不再随 `pending_review` 一起出现 |
| `stale` | 只含本镜输入变化（`script_sig`/`prompt_sha` 不符）；`stale_global`（编译器升级）只作独立字段，不进 `stale`、不上卡片 |
| `本机已收到` | 以 `takes.json` 的 `active` 指向文件在本机存在为准；有当前视频但文件不在本机的镜，状态写 `todo` + note“片子还没拉到本机”，不计 `final`/`pending_review` |
| `assembled`（成片已拼好） | 只认 `data/episodes/<run>/epNN_vN.mp4` 文件存在且新于最近一次视频定稿；phase 与 film 入口用同一布尔值，不再各算各的 |
| `eta` | 分道中位数（沿用 `jobs.eta_table()` 的分道口径，不用 `video_index._median_s()` 的不分道口径）；无样本时视频默认 840 秒（`jobs.py:511-512` 的 `ETA_DEFAULT_S`） |

**总览接口返回结构**（沿用子报告 §5.1，字段收紧后）：
```json
{
  "run": "series10_v2", "at": "…",
  "totals": {"script": [10,10], "first": [n,55], "last": [n,55], "video": [n,55], "cut": [n,4]},
  "videos": {"total": 55, "final": n, "rendering": n, "queued": n, "pending_review": n,
             "best_effort": n, "need_frames": n, "todo": n, "failed": n,
             "eta_all": "…", "median_s": 840.0},
  "episodes": [{
    "ep": 1, "title": "…", "total": 16,
    "phase": "frames|videos|assembly|done",
    "first_done": n, "last_done": n, "fallback_n": n, "video_done": n, "assembled": false,
    "film": {"name": "ep01_v3.mp4", "version": 3, "url": "/files/…",
             "kind": "preview|final|unknown", "missing_n": 2, "missing": ["ep01-S3"]} | null,
    "shots": [{
      "id": "ep01-S1", "index": 1,
      "first": {"state": "passed|ready|running|pending|failed|best_effort|fallback|na", "url": "/files/…", "n": 19},
      "last":  {"state": "…", "url": "/files/…", "n": 7, "fallback": {"mode": "i2va"}},
      "video": {"state": "…", "url": "/files/…", "n": 3, "progress": 0.42, "eta": "…", "queue_pos": 2},
      "review": "passed|rejected|null", "stale": ["…"], "stale_global": ["…"],
      "missing": ["first"], "candidates": 2
    }]
  }]
}
```
（`brain` 小节 v1 删除；`pipe` 六步矩阵由 jobs 表按 step 聚合现算，不再调 `sop_gate.stage_view`。）

### 2.5 数据目录布局（沿用现有 takes.json，让 ep01–ep04 已有成果不用重跑）

```
data/
  queue.db                        # 新增：SQLite 队列表（笔记本唯一）
  renders/<run>/epNN/SNN/         # 原样沿用，不动结构
    takes.json                    # 一镜一账本（active/takes/seq/last_op），写入口仍是 mutate_takes 语义
    takes/image_NNN.png / last_NNN.png / video_NNN.mp4
    frame.png / last.png / video.mp4    # 当前版副本，下游直接读
    compiled.json                 # 每镜编译结果（board 步产物）
    visual_spec.json / edits.json
    .takes_bak/                   # 备份留 10 份（沿用旧规则）
  pipeline/runs/<run>/
    P4_epNN.md                    # 剧本（只读输入）
    video/clips/epNN/epNN-SN__<变体>_s<seed>.mp4   # 视频片段路径约定保留
    video/specs/<sid>.spec.json   # 保留（变体规格，供 video 步读）
    video/prompts/<sid>.<变体>.txt
  episodes/<run>/epNN_vN.mp4      # 成片，保留
  import/runs/<run>/script_v2.json  # 剧本解析产物，编译输入，保留
  assets/...                      # 参考图、场景底图，保留
  brain/                          # v1 不再写（大脑面板删除）
```

**不再读 / 不再写的文件**（旧系统留下，新系统不认）：`data/producer/runs/<run>/registry.json`（推导登记表）、`control.json`、`attempts.json`、`leases/`、`master_rejects.json`、`gates.json`、`data/pipeline/runs/<run>/autoflow_epNN.json`、`data/pipeline/sop_gate/<run>/epNN_prompts.json`、`data/pipeline/asset_jobs/<run>/_ready_epNN.json`、`video/jobs.json`（视频作业状态进 SQLite；变体名可参考，不导入状态）、`video/state_<host>.json`、`night_state.json`、`episode_state.json`、`run_phase.json`、`_producer.json`。迁移时这些文件原地保留（不删除），只不再被新系统读取。

### 2.6 六步直线：每步一个小函数、输入输出写盘、挂了只重跑那一步

| 步 | 函数（新文件 `ns2/steps.py`） | 输入（磁盘 + jobs.payload） | 输出（磁盘 + jobs.result） | 失败处理 |
|---|---|---|---|---|
| 1. script 剧本 | `import_script(run, ep)` | `P4_epNN.md` | `data/import/runs/<run>/script_v2.json`；jobs `script` done | 解析失败 → `failed`，人工修剧本重跑。解析器复用 `importer/script_md.py` |
| 2. board 分镜 | `compile_shot(run, shot)` | `script_v2.json` + `config/visual_bible.toml` + `asset_refs` | `compiled.json`（`image_prompt` / `video_prompt` / `last_prompt` + `compile_trace.prompt_sha` + `bible_version`）+ frame_job 作业 dict | preflight 不过 → `blocked` + 缺失项清单（缺哪张参考图、缺哪个资产，写进 `error`）；资产补齐后人工重派。编译复用 `compiler.compile_shot()` + `frame_job.build_first()` |
| 3. first 首帧 | `render_first(run, shot, seed)` | `compiled.json` + 参考图 | takes.json 新增 `stage=image` 的 take（`workflow: qwen_edit/qwen_t2i`）+ `frame.png` 副本；`result={take_n, file, qc}` | ComfyUI 不可达/超时 → `failed`，指数退避重派（最多 3 次）；3 次用尽 → `failed` 待人工。QC（Q1–Q8 + Q9）只打分写进 `take.postflight`/`score`，不过也设为当前（取最高分版），状态由人工 review 定。执行复用 `unified_frame._run` + `comfy_client.run` |
| 4. last 尾帧 | `render_last(run, shot)` | 首帧当前版 + 落幅构图（`build_last_compose`） | takes.json 新增 `stage=last` 的 take + `last.png` 副本 | 首帧无当前版 → 不领活（等前一步 done）；L-204 D8 不用尾帧的镜记 `na`，跳过。执行复用 `frame_job.build_last_compose` + `unified_frame.render_last` |
| 5. video 视频 | `render_video(run, shot, variant)` | 首帧当前版 + 尾帧当前版（或 `i2va` 兜底）+ H3 提示词 + 变体规格 | `video/clips/epNN/<shot>__<变体>_s<seed>.mp4` + takes.json 新增 `stage=video` 的 take | 变体顺序固定 `v1 → s2 → s3 → s4 → mot`，最多 4 次；不过取 penalty 最小标 `best_effort` 等人工定。变体编译失败 → `blocked` + 缺失项清单（这是 A14 `needs_writer` 的新版定义：不许只写一个状态词，必须写缺什么）。执行复用 `h3_video.render` + `workflows.h3_fl2va` |
| 6. film 成片 | `assemble_episode(run, ep)` | 本集全部 video 当前版 | `data/episodes/<run>/epNN_vN.mp4`；`result={film, missing:[...]}` | 缺视频的镜用静帧占位，并在 `result.missing` 列出，不阻塞、不失败。v1 只拼视频 + 静帧占位 + 响度归一 -14 LUFS；TTS / 配乐 / 4K upscale 移到 v1.1。执行复用 `ns_post.assemble` + `ffmpeg` 最小集 |

**每步通用契约**：
- 输入 = 磁盘文件 + `jobs.payload` JSON；输出 = 磁盘文件 + `jobs.result` JSON。步与步之间只通过磁盘文件和 jobs 表传递，不直接调对方函数、不共享内存状态。
- 幂等：同一 `idem_key` 重复提交返回已有 job；worker 执行前先查 takes.json——该步已有当前版且输入签名未变，直接报 `done` 不重跑（不占新版本号）。
- 重试：失败只重跑那一步，上游产物不动；`max_tries=3`，infra 错误退避不计次（沿用 §1.4 第 4 条）。
- 成人镜：`owner_trigger=1` 的行 worker 不领；项目方在前端点“触发”后（调 `PUT /api/shots/{id}/review` 清掉标记），下一轮认领才可见。

### 2.7 并发、lane 与失败规则

- **每卡并发 1**：worker 每卡一个执行槽；同一 worker 一次只领一条 job（`capacity` 按卡数算，但执行是串行的每卡一条）。gpu33 可用道：0、1、3（**2 是坏卡，永不派**，沿用 `BAD_LANES=("gpu33:2",)`）；gpu36 道：0、1、2（视频优先 gpu36:2；长镜头 >260 帧只派 gpu36:2，不截短）。
- **道的选择**：派单时 `lane` 写死在 `jobs.payload` 里（按集分片：ep01/ep03 归 gpu33，ep02/ep04 归 gpu36——沿用现有 `partition.json` 的分集结果，迁移时一次性导入，不再动态算）；worker 只领属于自己机器的 lane 的活。v1 不做动态负载均衡。
- **审图不占出图道**：postflight Q1–Q8 沿用 `unified_frame._postflight` 的远端算、失败退本机 CPU；Q9 看图走现有 :8870 服务，但只在 worker 本卡空闲间隙调；审图实例数固定 1。**打分积压不设上限、不停派**，只记 `result.qc_pending`。
- **超时**：单步执行超时 45 分钟（沿用旧 `LEASE_S`），超时由心跳回收（§2.3）；视频步单镜上限 4 次变体（沿用旧阶梯），用完标 `best_effort`。
- **测试 run**：`run_id=test-<yyyymmdd-hhmm>`，3 镜迷你剧本沿用 `eval/smoke/P4_smoke.md` 的 3 镜夹具；进同一张队列表、按 `created_at` 排队；验收脚本（新 `ns2/smoke_check.py`）只读 jobs 表 + takes.json 判 8 项（G1–G8 判定语义沿用 `eval/smoke.py` 的 `judge_gN`，但删掉“翻 producing”、“占卡 25 分钟”、“翻回 paused”三段）。

### 2.8 新旧对照：每个旧卡死原因的解法

| 旧原因（§1.3） | 新解法 |
|---|---|
| 1. 优先级让路整拍归零、无老化 | 队列表没有 priority 字段；认领只按 `created_at` FIFO；测试与生产同表排队 |
| 2. 无单一事实来源、状态靠推导、三套口径 | jobs 表是派单唯一事实来源；takes.json 是产物唯一事实来源；总览口径表（§2.4）定死 8 项，不许三处各算 |
| 3. 四层常驻 + 双链路 + 双写入口 | 常驻只剩 API + worker 两类；出图只留 `unified_frame` 一套；视频只留 `h3_video.render` 一套；takes.json 写入口只留 `mutate_takes` 语义，旧 `_save_takes` 删除 |
| 4. 冒烟门与生产共用派单器、抢卡 | 测试是普通 run_id，进 FIFO 排队；验收脚本只读表和文件，不翻状态、不占卡、不跑 producer |
| 5. 闸门 fail-closed 在整集粒度、无逃生口 | 前置检查只挡那一镜的那一步（`blocked` + 缺失清单），其他镜继续跑；没有整集门、没有复盘门 |

---

## 3. 迁移方案

### 3.1 旧仓库封存

1. 在 `85f96a1f` 上打 tag：`archive/ns-final-20261108-85f96a1f`，推送到 GitHub。tag 后旧仓库不再接受新提交。
2. 仓库根目录加 `ARCHIVED.md`（一页）：写明封存日期、tag、封存原因（生产卡死、废弃重构）、新仓库地址、回滚启动命令（见 §3.4）。
3. 服务器 sv-20261107-13 的部署保持运行，直到 M1 一镜跑通；M1 之后停旧 producer/night/vidgen 的派单（进程停掉，不删除），只保留 :8800 旧 API 只读到 M3；M3 全量切过去后，旧常驻全部停掉，旧仓库在 GitHub 上设为 Archived（只读）。
4. **双写禁令**：从 M0 开始，只有新系统写 takes.json。旧 producer 必须先停（不停就存在两边 `seq` 冲突），再启动新 worker。并行期旧系统只读、不派单。

### 3.2 新系统放哪里：推荐新仓库

| 方案 | 做法 | 结论 |
|---|---|---|
| **A. 新仓库（推荐）** | 新建仓库（名待定，如 `ns-studio-v2`），只把 §1.2 里“保留”的约 15 个文件复制过去（`ns_render/comfy_client.py`、`workflows.py`、`unified_frame.py`、`pipeline.py` 的 `mutate_takes`/`set_active` 部分、`ns_agent/compiler.py`、`frame_job.py`、`gen_contract/`、`ns_recipes/compiler.py` + 3–4 个 yaml 配方、`ns_post/assemble.py` + `ffmpeg.py`、`importer/script_md.py`、`packages/schema/`、`comfy/models.lock`、tunnels.json 的端口映射表），其余全部新写 | **推荐**。旧仓库 3,296 文件、434 个测试、V1 legacy 路径、冻结期提交模板，全是包袱；新系统只复用 ~15 个文件，git 历史连续的价值几乎为零。新仓库从第一天就是干净的、只含生产需要的东西 |
| B. 同仓库新目录 | 旧仓库里建 `ns2/` 目录，新系统放里面，旧代码原地不动 | 备选。唯一好处是 git 历史连续；坏处是新系统与 3,296 个旧文件同处一仓，旧测试、旧 CI、旧冻结规则会继续干扰（`tests/` 434 个文件里大量测试 import 旧模块），且“封存”与“新开发”混在一起，分不清哪边是事实来源 |

### 3.3 旧数据迁移：断点续跑（`ns2/migrate.py`，M0 写）

迁移只读三样东西：takes.json（产物）、review.json（定稿结论）、`script_v2.json`（剧本结构）。其他状态文件一律不导入。

```
步骤：
1. 扫 data/renders/series10_v2/ep*/S*/takes.json（55 镜）
2. 每镜每步校验：
   · active 指向的 file 存在 且 postflight.ok（或 review passed）→ 该步记 done
   · active 指向的 file 不存在 → 记 missing，重新入队（queued）
   · 无 active → 该步记 queued
   · review.json passed 的步 → 记 done（即使 postflight 缺口径，以人工定稿为准）
   · L-204 D8 不用尾帧的镜 → last 步记 na
   · 视频步：以 takes.json stage=video 为准；video/jobs.json 只读变体名做参考，
     不导入它的状态；state_<host>.json 不读
3. 生成 jobs 表初始行：done 的行直接写 done（不进队列）；queued 的行按
   created_at = 迁移时间 + 镜顺序号进队（ep01 先、ep02 其次，集内按镜号）
4. 输出校验报告（每镜每步 done/missing/queued 清单 + 总数），用户确认后再开新派单
5. runs 表写入 series10_v2 一行；partition 分集结果（ep01/ep03→gpu33，ep02/ep04→gpu36）
   一次性写进 jobs.payload.lane，不再动态算
6. models.lock 的 LoRA 清单原样复制；其中 sha256 为 null 的 6 条（含 video 的
   lin_wan_h3_v1，status: training 未交付）先标“未校验”，渲染前校验、不一致拒绝；
   models.lock 里 purelens_qwen21 出现两次且 sha256 不同——迁移时以
   data/ 下实际文件算出的 sha256 为准，多余的一条删除（未核实哪条为准，见附录）
```

**不迁移的东西**：`registry.json` / `control.json` / `attempts.json` / `leases/` / `master_rejects.json` / `gates.json`（推导状态，不迁移）；`autoflow_epNN.json` / `epNN_prompts.json` / `_ready_epNN.json`（SOP 证据，不迁移）；`video/jobs.json` / `state_<host>.json` / `night_state.json`（视频子系统状态，不迁移）；`episode_state.json` / `run_phase.json` / `_producer.json`（阶段中心状态，不迁移）。这些文件原地保留、不删除，只不再被读取。

**迁移后 ep01–ep04 的起点**（按 11-08 03:45 仪表盘 + STATE.md，迁移时以校验报告为准）：首帧 55/55 done、尾帧 50/55（缺 5 镜进队）、视频 30/55（缺 25 镜进队，其中 ep02 的 15 镜尽力版、ep04 的 5 镜尽力版先由用户在前端定稿——定了的记 done，没定的留在 `best_effort` 等人工，不自动重跑）。

### 3.4 回滚办法

- **回滚触发**：M1 验收失败、或 M2 整集连续 2 天无产出、或用户一句话。
- **回滚操作**（30 分钟内可完成）：
  1. 停新 worker（systemd stop）+ 停新 API（:8800 切回旧 API 进程）。
  2. 按 `ARCHIVED.md` 里的命令重启旧常驻：`producer.py serve` + `night.py` + 各机 `vidgen.py`（启动脚本保留在旧仓库 `ops/` 下，不删除）。
  3. 前端切回旧 API（改回 5 个旧接口调用——前端改动保留在一个独立 commit 里，回滚就是 revert 那个 commit）。
- **数据回滚**：不需要回滚数据。takes.json 写入口语义不变（`mutate_takes` + `seq+1` + 备份留 10 份），新系统写入的 take 版本号继续递增、不覆盖旧版；回滚后旧系统读到新版 take 时按“未知签名”处理（`take_sig` 默认 `record` 模式，只记不换代）。jobs 表是新系统的私有状态，旧系统不读它，回滚时原地保留、不删除。
- **回滚演练**：M1 验收里包含一次完整回滚演练（切过去 → 跑一镜 → 切回来 → 再切过去），演练通过才算 M1 完成。

---

## 4. 实施顺序与里程碑

工作量单位是“能独立干活的工程师天数”（1 个人全职做，下同；不含用户定稿等待时间）。总计 **11–16 工程师天**，单人约 2–3 周日历时间。M1 之后按实际进度重估一次。

### M0 — 准备 + 封存 + 迁移脚本（1–2 天）

- 做什么：旧仓库打 tag + 写 `ARCHIVED.md`；建新仓库，复制 §3.2 的 ~15 个保留文件；写 `ns2/migrate.py`；跑校验报告（55 镜 × 6 步的 done/missing/queued 清单）。
- 完成标准：① tag 已打并推送；② 新仓库可 clone，保留文件能 import；③ 校验报告已出，用户已确认“哪些步 done、哪些步要重跑”（报告里总数与 11-08 仪表盘对得上：首帧 55/55、尾帧 50/55、视频 30/55，差值要能解释）。
- 工作量：1–2 天。

### M1 — ep01-S1 一镜跑通新系统（3–5 天）

- 做什么：新 API（§2.4 的 8 个接口）+ SQLite 队列表 + 两台 worker + claim/heartbeat/finish；`first` / `last` / `video` 三步函数（复用 `unified_frame` + `h3_video`）；18800 隧道转发；`ns2/smoke_check.py` 验收脚本初版。
- 完成标准：① ep01-S1 从 `queued` 到 `video done` 全程只走新系统，旧 producer 全程停着；② 同输入重复提交一次，0 新版本文件（`idem_key` 生效）；③ 回滚演练一次通过（切回旧系统 → 再切回新系统，takes.json 无冲突）；④ L-205 单镜耗时可观测（jobs 表有 started/finished 时间）。
- 工作量：3–5 天（最难的一步是 worker 跨机调 API 的隧道与认领原子性，留足时间）。

### M2 — 整集 ep01 跑通（3–4 天）

- 做什么：ep01 剩余 15 镜进队跑完；`film` 步（assemble 最小集）上线；前端 5 处调用点改完（§2.4）；`board` / `script` 两步上线（ep05 剧本进来时用）。
- 完成标准：① ep01 16/16 视频 done 或 `best_effort`（人工已定）；② 成片 `ep01_vN.mp4` 出片，可播放、时长与剧本一致（±2 秒）；③ 前端总览页只调 `/api/overview`，数字与 jobs 表 + takes.json 一致（抽查 5 镜，0 口径差异）；④ 单集耗时记进 jobs 表，可对照 L-205 的 50 分钟预算出归因（逻辑 / 代码 / GPU 调度 / 检测四类）。
- 工作量：3–4 天。

### M3 — 全量 4 集断点续跑（2–3 天）

- 做什么：ep02–ep04 按迁移报告进队跑完；尽力版人工定稿流程跑通（前端 review 接口 + `best_effort` 清单）；旧 :8800 API 下线，旧常驻全部停掉。
- 完成标准：① 55 镜状态与迁移报告一致，0 差异（校验脚本再跑一次）；② 视频 30/55 → 55/55 done 或 `best_effort`（人工已定）；③ 旧常驻停掉后 24 小时无人工干预仍有产出；④ 新 `smoke_check.py` 跑通 8/8（测试 run_id 进 FIFO，不翻状态）。
- 工作量：2–3 天。

### M4 — 收尾 + 文档 + 冻结新规则（2 天）

- 做什么：删旧常驻启动脚本里的自动拉起（保留手动回滚命令）；新 STATE.md 规则（一页，≤80 行，沿用旧 CLAUDE.md 的“每会话更新 STATE.md”这一条，其他规则废弃）；`ns2/smoke_check.py` 定稿；写一份一页的操作手册（怎么派单、怎么定稿、怎么回滚、卡住了看哪三处：jobs 表、takes.json、worker 日志）。
- 完成标准：① 操作手册已写，用户能照着派一单；② 回滚命令实测可用；③ 旧仓库 GitHub 设为 Archived。
- 工作量：2 天。

### 里程碑总览

| 里程碑 | 完成标准（一句话） | 工作量 |
|---|---|---|
| M0 准备 + 封存 + 迁移脚本 | tag 已打；校验报告用户已确认 | 1–2 天 |
| M1 ep01-S1 一镜跑通 | 一镜全程走新系统；同输入 0 新版本；回滚演练通过 | 3–5 天 |
| M2 整集 ep01 跑通 | ep01 16/16 视频落定；成片出片；前端只调一个接口 | 3–4 天 |
| M3 全量 4 集断点续跑 | 55/55 落定；旧常驻停掉 24h 仍有产出；新验收 8/8 | 2–3 天 |
| M4 收尾 + 文档 | 操作手册 + 回滚实测 + 旧仓库 Archived | 2 天 |
| **合计** | | **11–16 天** |

---

## 5. 明确不重建的东西 + 新系统第一版刻意不做的功能

### 5.1 不重建（旧架构里的东西，新系统里没有）

- Temporal / DAG / 任务图（L-209 的 S0–S4 路线图作废；新系统没有 DAG，只有 FIFO 队列表）
- `sop_gate` 九阶段门、复盘门、promote 状态机、observe/enforce 双模式、退避签名、`import_revalidate`
- 冒烟门挡生产（`eval/smoke.py` 的 G1–G8 不再作为生产门禁；只留改写后的离线验收脚本）
- agent-memory（大脑面板、`data/brain/`、`decisions.jsonl`、`snapshot.json`、BrainPanel）
- 常驻进程动物园（resident_sync / autoflow / night / producer / gpu36 看图 / health_tick / idle_watch / watchdog / api_watchdog；只留 API + worker + 隧道监管）
- producer 的 rung 阶梯 / redo 判定 / 打分积压动态上限 / 跨优先级让路 / registry 推导 / 合并登记表 / lease 租约文件（由 SQLite 原子认领 + 心跳回收代替）
- 多剧并行（`runs.py` 登记表、legacy 路径、V1 只读冻结逻辑；v1 只有 series10_v2 + 测试 run）
- `agent_dispatcher`（默认关的无头 Claude 派单）、`api_watchdog`（只记不重启）
- 旧 pipeline 出图路径（`render_image` C5 多轮多候选）、`score.py` 旧评分、`packages/qc` 重叠打分、`frame_spec` 的 LLM 可选补全
- 看图多实例管理（`review_instances.sh` 的 scale；审图实例固定 1）
- 发版流水线（`release_tick.py` 的 cut/deploy/handover；v1 手动部署）
- inbox / owner queue（成人触发改成 review 接口里的一个按钮，不再维护独立的待办队列）
- `state_paths.py` 的新旧双位置兼容、`runs.py` / `run_ctx.py` / `run_paths.py` 三处路径解析（新系统路径写死相对路径）

### 5.2 新系统第一版刻意不做的功能（v1.1 以后再议）

- 自动写作推进（autoflow：`.done` 的集自动往下写；v1 剧本由人工写好放进 `P4_epNN.md` 再调 `script` 步）
- TTS / 配乐 / 4K upscale（成片 v1 只拼视频 + 静帧占位 + 响度归一；TTS、配乐、4K 移到 v1.1）
- 自动定稿锁（过契约自动设为当前保留，但“定稿”只由人工在前端点；没有自动锁）
- 动态负载均衡 / GPU 池注册 / 心跳中心服务（lane 写死在 job 里；v1 不做调度优化）
- 多剧并行（series10_s2 不迁移；第二季要开时再加 run，不改架构）
- 新前端（前端原样跑，只改 5 处调用点；画布审片、胶片模式、筛选逻辑全部不动）
- 打分模型升级（Q1–Q9 原样用；QC 只打分不拦路的口径不变）
- 速率限制与 24 小时硬上限的时间窗口版（v1 只留 `max_tries=3` + 人工 `blocked`；跑飞时靠人工停。M4 复评是否加回一个全局每日上限——这是有意简化，不是遗漏）

---

## 6. 需要用户拍板的决策点

| # | 决策点 | 选项 | 我的推荐 | 理由（一句话） |
|---|---|---|---|---|
| 1 | **范围：只换后端留前端和数据 vs 全换** | A. 只换后端（前端保留、数据原样沿用 takes.json） / B. 全换（含前端重写） | **A** | 前端 223 文件 43,053 行不是卡死原因；重写前端是另一个项目，会把“最快恢复生产”拖成“重做产品”。前端的 bug 是口径问题，修 5 处调用点 + 2 处口径即可 |
| 2 | **新系统位置：新仓库 vs 同仓库新目录** | A. 新仓库 / B. 同仓库 `ns2/` 目录 | **A** | 旧仓库 3,296 文件 + 434 个测试 + 冻结规则全是包袱；新系统只复用约 15 个文件，git 历史连续的价值几乎为零。新仓库从第一天就是干净的 |
| 3 | **series10_v2 在途 4 集：原样迁移断点续跑 vs 从第 5 集全新开始** | A. 原样迁移（§3.3） / B. 旧 4 集不要了，从 ep05 全新开始 | **A** | ep01–ep04 已有首帧 55/55、尾帧 50/55、视频 30/55 是真实资产，丢掉等于重烧 GPU 时间和已付的定稿。takes.json 是唯一事实来源且可校验，不存在“旧数据不可信”的问题。选 B 唯一的理由是“旧数据不可信”，目前没有证据支持 |
| 4 | **测试与生产是否同队列** | A. 同一张队列表、不同 run_id、按时间排队 / B. 测试单独建队列/单独占卡 | **A** | B 就是旧架构死锁的复刻（测试占卡抢生产）。测试是普通任务，老实排队；验收脚本只读表和文件，不占特权 |
| 5 | **成片 v1 是否含 TTS / 配乐 / 4K** | A. 不含（先出无声占位成片恢复生产） / B. 含（等 TTS 接好再出成片） | **A** | 成片的核心是“视频拼起来能看”；TTS 和配乐是加分项，不该挡住恢复生产。v1.1 再加，接口已预留（`film` 步的 result 可扩展） |
| 6 | **旧系统停机时点** | A. M1 一镜跑通后停旧派单，M3 后全停 / B. 现在就全停 / C. 新系统全量跑通后再停 | **A** | B 太冒险（新系统还没证明能跑）；C 太保守（双系统并行越久，双写冲突风险越大）。M1 已证明新系统能出一镜，就可以停旧派单；M3 全量切过去后全停 |

---

## 附录：未核实清单（不要当作事实用）

1. `data/` 不在仓库中，本计划 §2.5、§3.3、§4 中所有落盘文件（`registry.json`、`control.json`、`jobs.json`、`takes.json` 实例、`video/state_<host>.json` 等）的实际存在与字段齐全性，均为按代码推断，未与真实文件逐字段核对。M0 的校验报告是第一次用真实文件核对的机会。
2. 11-07 22:45 `priority_yield` 日志与 `runs.json` -1 的对应关系（见 §0）。
3. `take.producer.lane` 的写入点：G4 依赖该字段，但 `Take` 模型中无此字段，写入位置未找到。
4. `takes.json` 顶层 `last_frame_fallback` 字段：只在 SPEC 与 `build_jobs.py` 中出现，在 `pipeline.py` 中未核实。
5. `config/pipeline/state.json` 本体不存在，`take_sig` / `episode_closed` / `run_phase` 的实际生效 mode 未核实（只知默认 `record`）。
6. gpu36 的卡数与完整道表未核实（只知视频道 gpu36:2、审图走 8870/8871）。
7. `vidgen.py` 自带 ComfyUI 客户端与 `ns_render.comfy_client` 的协议等价性未逐行比对（新系统只留后者，前者随 vidgen 一起扔掉，影响仅限于迁移期对旧视频状态的解读）。
8. `comfy/models.lock` 里 `purelens_qwen21` 出现两次且 sha256 不同，哪条为准未核实；`loras` 6 条 sha256 全为 null 未回填。
9. 全量测试在 HEAD 是否通过未核实（本轮未跑测试，任务亦要求只读）。
10. L-200 原文未逐条核实（仅见多处引用）；`NowPanel`（“现在四问”）内部是否还读其他接口未逐行读完；`jobs` 路由组内 cancel 等写接口的具体路由行号未核实；`shot_status.main5` 全文细节未核实。

---

*计划完成。下一步：等用户拍板 §6 的 6 个决策点（至少 1–3 项），然后按 §4 从 M0 开工。*

---

## P5 修正（项目方 11-08 “开工”批准，优先于上文）

1. **API + queue.db 放 gpu33**，不放笔记本。gpu36 worker 经内网直连 10.0.0.33；笔记本只经现有隧道开前端。取消 18800 反向隧道。
2. **出图 / 视频分区分开**（`config/netfix/partition.json` 实测）：images ep1,3,5,7,9,10→gpu33 / ep2,4,6,8→gpu36；video ep3,5,7,9,10→gpu33 / ep1,2,4,6,8→gpu36。
3. **data/ 已核**（M0 实测，非推断）：权威副本 = 干这一步的那台服务器；笔记本副本落后。
4. **QC 不过只挡本镜下一步**（§1.4 #12），不反压派单；不把未过 QC 的帧送进视频。
5. **gpu33 GPU3 专给审图**，gpu33 视频道 = 0、1。
6. **（M0 新发现）视频尽力版不在 takes.json**：只在旧视频子系统 `state_<host>.json`（24 镜）。§3.3 “state_<host>.json 不读”作废：迁移必须并读，M1 起视频结果写回 takes.json，单一来源从新系统开始成立。
7. **复用件实际闭包 101 文件 / 36k 行**（不是 ~15 个）：M0 原样按原相对路径复制，M2 后再瘦身。
8. 旧仓库封存 tag 为 `archive/ns-final-20261108`（指向 be08e778，含 A14 基线记录），不是 85f96a1f。
