"""配方引擎（ARCH 2026-10-31）。

Shot Spec（packages/schema/ns_schema/shot_spec.py）+ 配方（services/recipes/recipes/*.yaml，每个参数带 provenance）
→ selector 选配方 → compiler 产出确定性的 RenderPlan（ComfyUI API 图 + 提示词 + 后处理 + 验收阈值 + plan_hash）。
纯函数、可重放、可测试；不读 GPU 状态，不提交任务（提交由 tools/iterate_loop 负责）。
"""
COMPILER_VERSION = "ns_recipes/1.0"
