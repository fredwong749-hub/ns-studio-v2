"""ns_qc — measurable production gates for recurring problems (docs/production_issues.md PI-01…PI-30).

Every gate is technical (sizes, hashes, PSNR/SSIM, ΔE, SFace cosine, LUFS, F0, versions, file ages); no gate judges
content or aesthetics (project rules G1–G6). Thresholds live in `thresholds.py`, each with its documentary source;
an undocumented threshold is marked 待项目方定 and only warns.

Dependencies: stdlib + numpy + OpenCV (+ Pillow for reading images) + the ffmpeg/ffprobe binaries. The package is
self-contained (it does not import other NS-Final Studio services) so the gpu3 side can vendor or import it as is.
See docs/qc_gates.md.
"""

QC_VERSION = "ns_qc/2026-10-30.1"
