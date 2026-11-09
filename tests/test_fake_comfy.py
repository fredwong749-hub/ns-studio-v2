"""审查 3 M4：真 HTTP 假 ComfyUI——不 monkeypatch submit / wait / upload / cancel，走 step_video 里的真实请求代码"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeComfy:
    """够 step_video 用的 ComfyUI 子集：/upload/image、/prompt、/history/{id}、/queue（GET / POST delete）、/interrupt"""

    def __init__(self):
        self.uploads, self.prompts, self.deleted, self.interrupts = [], {}, [], 0
        self.running = None              # 正在跑的 prompt_id
        self.mode = "ok"                 # ok | error | noout | hang
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/upload/image":
                    name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                    fake.uploads.append(name)
                    return self._send({"name": name, "subfolder": "ns2", "type": "input"})
                if self.path == "/prompt":
                    d = json.loads(body)
                    pid = f"p{len(fake.prompts) + 1}"
                    fake.prompts[pid] = d["prompt"]
                    fake.running = pid
                    return self._send({"prompt_id": pid, "number": 1, "node_errors": {}})
                if self.path == "/queue":
                    fake.deleted += json.loads(body).get("delete") or []
                    return self._send({})
                if self.path == "/interrupt":
                    fake.interrupts += 1
                    fake.running = None
                    return self._send({})
                self._send({}, 404)

            def do_GET(self):
                if self.path == "/queue":
                    return self._send({"queue_running": [[0, fake.running]] if fake.running else [], "queue_pending": []})
                if self.path.startswith("/history/"):
                    pid = self.path.rsplit("/", 1)[1]
                    if fake.mode == "hang" or pid not in fake.prompts:
                        return self._send({})
                    msgs = [["execution_start", {"timestamp": 1000}], ["execution_success", {"timestamp": 43000}]]
                    if fake.mode == "error":
                        return self._send({pid: {"status": {"status_str": "error", "messages": [["execution_error", {"node": "9"}]]}}})
                    outs = {} if fake.mode == "noout" else {"9": {"videos": [{"filename": "ns2_x_00001.mp4", "subfolder": "ns2"}]}}
                    return self._send({pid: {"status": {"status_str": "success", "completed": True, "messages": msgs}, "outputs": outs}})
                self._send({}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def comfy(monkeypatch, tmp_path):
    from ns2 import step_video as SV
    f = FakeComfy()
    monkeypatch.setitem(SV.PORTS, "gpu33:0", f.port)
    monkeypatch.setitem(SV.OUTDIR, "gpu33:0", "/out/gpu0")
    monkeypatch.setattr(SV, "ROOT", tmp_path)
    SV._uploads.clear()
    yield f
    f.srv.shutdown()


def _var(tmp_path):
    (tmp_path / "frames").mkdir(exist_ok=True)
    (tmp_path / "frames/first.png").write_bytes(b"\x89PNG fake")
    return {"graph": {"1": {"class_type": "LoadImage", "inputs": {"image": "@first"}},
                      "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
                      "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "x"}}},
            "inputs": {"@first": "frames/first.png"}}


def test_submit_upload_wait_real_http(comfy, tmp_path):
    from ns2 import step_video as SV
    var = _var(tmp_path)
    pid = SV.submit("gpu33:0", var, "ns2/ep01-S1__v1", {"agent": "NS2"}, seed=777)
    g = comfy.prompts[pid]
    assert g["1"]["inputs"]["image"].startswith("ns2/ns2_") and len(comfy.uploads) == 1        # 真上传、引用换成 Comfy 文件名
    assert g["2"]["inputs"]["noise_seed"] == 777 and g["9"]["inputs"]["filename_prefix"] == "ns2/ep01-S1__v1"
    SV.submit("gpu33:0", var, "p", {}, None)
    assert len(comfy.uploads) == 1                                                              # 同文件同指纹不重传
    (tmp_path / "frames/first.png").write_bytes(b"PNG changed, longer")
    SV.submit("gpu33:0", var, "p", {}, None)
    assert len(comfy.uploads) == 2                                                              # 重渲后指纹变 → 重传
    r = SV.wait("gpu33:0", pid, timeout_s=10, poll_s=0.05)
    assert r == {"path": "/out/gpu0/ns2/ns2_x_00001.mp4", "gpu_s": 42.0}


@pytest.mark.parametrize("mode,err", [("error", "comfy_error"), ("noout", "no_output")])
def test_wait_error_branches_cancel_real_http(comfy, tmp_path, mode, err):
    from ns2 import step_video as SV
    pid = SV.submit("gpu33:0", _var(tmp_path), "p", {}, None)
    comfy.mode = mode
    r = SV.wait("gpu33:0", pid, timeout_s=10, poll_s=0.05)
    assert r["infra"] and r["error"].startswith(err) and pid in comfy.deleted and "deleted" in r["cancel"]


def test_wait_timeout_cancels_and_interrupts_running(comfy, tmp_path):
    from ns2 import step_video as SV
    pid = SV.submit("gpu33:0", _var(tmp_path), "p", {}, None)
    comfy.mode = "hang"
    r = SV.wait("gpu33:0", pid, timeout_s=0.3, poll_s=0.05)
    assert r["error"] == "timeout" and r["cancel"] == "deleted,interrupted" and comfy.interrupts == 1


def test_cancel_not_running_only_deletes(comfy):
    from ns2 import step_video as SV
    comfy.running = "someone-else"
    assert SV.cancel("gpu33:0", "p9") == "deleted" and comfy.interrupts == 0                  # 别人的正在跑的不打断
