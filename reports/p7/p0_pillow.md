# P0：requirements.txt 补 Pillow（审查 3 §6）

- 问题：`4fa1f95` 的 `ns2/legacy_rules.py` 的 `board_no_text()` 与 `tests/test_gates.py` 都用了 `from PIL import Image`，但 requirements.txt 没有 Pillow。按 README 在全新 venv 里跑测试是 61 过 / 1 败。
- 改法：requirements.txt 加 `Pillow==12.0.0`。
- 验证：新建全新 venv（`python -m venv`，再 `pip install -r requirements.txt`），跑 `python -m pytest tests/ -q`，结果 **62 passed / 0 failed**，原始输出见 `reports/p7/p0_pillow_pytest.txt`。
- 服务器：执行层跑在旧部署 venv（`offline_20261101/venv`）里，那里本来就靠 Pillow 生成参考板，所以生产不受影响。SSH 恢复后补核 Pillow 版本。
