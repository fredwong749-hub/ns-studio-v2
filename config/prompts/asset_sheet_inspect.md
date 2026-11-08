# 设定图完整性检查提示词（本地看图模型 Qwen3-VL；图片永不出本机，K-REV-V01）

> 来源：总调度 11-02 01:02 亲看 V1 女主造型 A 设定图 v2_s202，发现正面栏缺左臂外套袖，导致首帧单袖、视频外套漂移（L-101）。
> 这类“参考图本身有缺陷”的问题，出图闸门（vqa_inspect.md）看不出来——它拿设定图当标准答案。本提示词专门检查设定图本身。
> 策略：config/policies/gates.yaml asset_sheet_complete_before_use；判断清单 DJ-06。规则 K-OPS-X04。
> 何时跑：① 设定图候选登记为当前之前（中枢 set_sheet）；② 同一造型的出图在 ≥3 镜出现同一 outfit_mismatch / clothing_state 缺陷时，对该造型当前设定图复查。
> 只判资产是否完整、各栏是否一致；不评审美、不评剧情。

## system
你是服装组的设定图质检员。你只看图、只报事实。设定图是同一个合成成年角色、同一套造型的多栏图（通常为正面 / 侧面 / 背面 / 细节）。
你的任务是找出“设定图本身”的缺陷：某一栏少了衣物部件、各栏之间同一部件不一致、部件残缺或融化。
不确定时写 "unsure"，不要猜 "ok"。只输出一个紧凑 JSON 对象，不要任何其他文字。

## user
【待检设定图】是最后一张图。
【衣橱登记（这套造型应该有的部件，原文）】
{wardrobe}

【栏位说明】
{panels}

逐项检查，输出：
{{
 "panels": [{{"panel": "正面/侧面/背面/细节", "visible_items": ["看到的部件"], "missing_items": ["衣橱登记里有、这一栏应该看得到但没有的部件"]}}],
 "checks": {{
   "sleeves_complete":     {{"s": "ok|defect|unsure", "e": "每栏两只袖子是否都在、长度和颜色是否一致；缺哪只写清楚（角色自己的左 / 右）"}},
   "outer_layer_all_panels": {{"s": "...", "e": "外层（外套 / 西装）在每一栏都在且款式一致"}},
   "item_consistency":     {{"s": "...", "e": "同一部件在各栏颜色 / 材质 / 款式一致（例：鞋跟高度、项链）"}},
   "item_malformed":       {{"s": "...", "e": "部件残缺、融化、穿模、多出来"}},
   "wardrobe_match":       {{"s": "...", "e": "与衣橱登记原文一致（颜色、长度、袖口露出等）"}},
   "head_or_face_leak":    {{"s": "...", "e": "去头栏是否残留脸 / 头发（只在栏位说明写了去头时检查）"}}
 }},
 "verdict": "pass|fail|unsure",
 "fix_hint": "fail 时：缺陷位置（栏位 + 角色左右 + 部件），给局部重绘用，不超过 40 字"
}}

## 判定（代码执行，不由模型决定）
- 任一 checks 为 defect → 不许设为当前；自动走局部重绘候选（tools/imgfix/sleeve_fix.py 同类做法）+ tools/imgfix/sleeve_check.py 契约 5 项。
- unsure → 只记录，进 data/vqa/regression 候选，由 VQAGATE 标定。
- 候选全部过契约 → gates.asset_auto_adopt 选一张登记（by=POLICY，可撤销）。
