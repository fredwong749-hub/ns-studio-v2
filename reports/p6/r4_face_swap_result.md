# R4 质量 2b：专门换脸模型离线 A/B（项目方 11-10 选“2”）——**效果过线，进生产前要项目方定许可证**

## 为什么选 inswapper，不选 PuLID / InstantID

旧仓库的调研（docs/ep02_iter/consistency_research.md）结论：
- PuLID / InstantID 只有 Flux / SDXL 版本，没有 Qwen-Image 版，接不进现在的出图链。
- ComfyUI 的 ReActor 节点内置裸露检测，会拦成人镜。

所以这次直接用它的底层模型做实验：
- 换脸：inswapper_128。
- 修复：GFPGAN 1.4 / CodeFormer。

实验方式：
- 模型用 hf-dl 从 facefusion/models-3.0.0 下到 gpu33 的 `/root/work/fredw/facelab/models`。
- 跑在 CPU 上（venv_idm，只读用），不动 ComfyUI、不碰队列、不写 takes。
- 脚本：`experiments/r4_face_swap.py`，分 prep / swap / score 三段。

## 样本与臂

- 样本：与质量 2 相同的 11 张 ep05 Q1 没过帧（首帧 3 张、尾帧 8 张，5 个角色）。
- 臂：
  - swap：只换脸。
  - swap_gfpgan / swap_codeformer：换脸后修复，blend 0.6。
  - **swap_cf_match**：换脸后用 CodeFormer 修复，再按“脸区清晰度最接近原图”自动选混合比例（0–1）。原图在焦外的脸保持焦外，在焦内的脸不被 128 px 换脸糊掉。

## 结果

| 臂 | 生产 Q1 过（≥0.6） | Q1 平均增量 | 另一个模型 SFace 平均（原 0.485） | 脸区清晰度不低于原图 80% |
|---|---|---|---|---|
| swap | 11/11 | +0.456 | 0.721 | 0/11（中位只剩 38%） |
| swap_gfpgan | 11/11 | +0.429 | 0.704 | 10/11（焦外的脸被修得过锐，最多 2.35×） |
| swap_codeformer | 11/11 | +0.424 | 0.702 | 9/11（最多 2.70×） |
| **swap_cf_match** | **11/11** | **+0.438** | **0.707** | **11/11** |

- 修前只有 1/11 过 Q1。
- 关于 SFace 这一列：生产 Q1 用的 ArcFace 和 inswapper 是同一家族的嵌入，换脸天然会把 Q1 分数刷高。所以另用视频门在用的 YuNet + SFace（独立的模型）复核。11 张全部变像，平均从 0.485 升到 0.707，说明不只是在迎合打分器。
- 脸外区域改动为 0。
- cf_match 选到的混合比例在 0.3–0.75 之间。

看图：
- `r4_face_swap_sheet.jpg`：左原图，右 swap_cf_match。
- `r4_face_swap_arms_sheet.jpg`：原图 / swap / gfpgan / codeformer 四列对比。

结论：
- 换出来的脸自然，表情、视线、光线保留，焦外的脸仍是焦外，不是磨皮塑料脸。
- 小瑕疵：
  - 换过的脸颗粒感比原图少。下一步可以按原图噪声补颗粒。
  - 焦外的人（第 4 行）比原图略清晰一点。

## 进生产前要定的事（不自动做）

1. **许可证（红）**：inswapper_128 和 buffalo_l 都是 insightface 的“非商业研究”许可。生产 QC 里本来就在用 buffalo_l 打分，但打分只是内部 QC；拿 inswapper 去改成片画面是另一回事。如果剧要商用，需要项目方确认，或另找可商用的换脸模型。
2. **接法（建议）**：
   - 做成出图后的 Q1 补救步：首帧 / 尾帧 Q1 没过时先跑一次 swap_cf_match，再过一遍全套 postflight。
   - 换脸版作为新的 take 记账（source=faceswap），原版保留，可回滚。
   - 开关默认关，先在下一集上 A/B，看首帧一次过率和尽力版占比的变化。
   - 成人镜同样只在自有机器上跑。inswapper 本身没有内容过滤，不会拦。
3. **对视频的影响**：I2VA 从首帧出视频，首帧换好脸之后视频身份应跟着变好，但要实测视频门的“身份偏离”。
