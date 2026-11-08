"""最小 ComfyUI HTTP 客户端（只用标准库 + PIL）：校验、上传、提交、等待、下载。

fredw-92/F1：笔记本 → gpu3 上行经跳板只有 8–25 KB/s，重传帧会超时。所以：
- download() 时把输出在 gpu3 上的名字（subfolder/filename、哪个 ComfyUI）按内容 sha256 记进 RemoteIndex；
- 之后同一个 ComfyUI（按 --output-directory 判定，两个实例目录相同也算）要用这张图时，
  LoadImage 直接写 "subfolder/name.png [output]"（ComfyUI 0.42 实测可用），不上传；
  需要缩放的（input_image(size=…)）在图里补一个 ImageScale；
- 提交时 LoadImage 报 Invalid image file（gpu3 上文件没了）→ 自动改为上传再提交一次；
- 真要上传：不透明图转 JPEG q95、按原内容 sha 命名、先 HEAD 查输入目录（两实例共用）已有就跳过、超时 ≥ 900 s。
"""
import hashlib
import http.client
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# 这些输入名对应模型文件，校验时要在 ComfyUI 的可选列表里
MODEL_INPUTS = ("unet_name", "clip_name", "vae_name", "lora_name", "model_name")


class ComfyError(RuntimeError):
    pass


class ComfyUnavailable(ComfyError):
    """连不上 ComfyUI（进程没起或被杀）：不重试、不换种子，整批停下"""


UPLOAD_TIMEOUT_S = 900          # 上传超时下限（上行 8–25 KB/s 时 3 MB 要 2–6 分钟）
UPLOAD_JPEG_QUALITY = 95
OUTPUT_TAG = " [output]"        # LoadImage 注解：从该实例的输出目录读
# NETFIX 11-02 00:0x（总调度派“显卡使用率太低”根因；CONSENT REFLOCK → NETFIX）：/object_info 2,683,903 字节（gpu36 实测），
# producer 每单新建 ComfyClient → 每单经跳板（单连接 36–53 KB/s）重拉一次 = 每单 1–3 分钟只在传节点表，GPU 空等。
# 改为进程级 + 磁盘缓存（按地址，TTL 6 小时）；检查发现缺节点 / 缺模型时先强制重拉一次再报，避免旧缓存误报。
OBJECT_INFO_TTL_S = 6 * 3600
# DEBUG-RSI 11-02（K-OPS-E01 / R02，总调度 fredw-2b 派）：经跳板时 /history 轮询偶发超时、/view 下载 IncompleteRead。
# 原来一次就整单作废 → producer 当 infra / content 错重派 → 已在 GPU 上出好的图白做一遍（ep01-S8 00:22:53 出好、00:24:33 轮询超时重派；
# ep01-S7 00:20:47 出好、00:22:23 下载 IncompleteRead 记一次内容失败）。现在：轮询连续失败 ≤ POLL_GRACE_S 内继续等；下载重试 DOWNLOAD_TRIES 次。
POLL_GRACE_S = float(os.environ.get("NS_COMFY_POLL_GRACE_S", "300"))
DOWNLOAD_TRIES = int(os.environ.get("NS_COMFY_DOWNLOAD_TRIES", "3"))
_OBJECT_INFO_MEM: dict[str, tuple[float, dict]] = {}
_OBJECT_INFO_LOCK = threading.Lock()


def _object_info_disk(base: str) -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "data" / "cache" / "comfy_object_info" / (hashlib.sha256(base.encode()).hexdigest()[:16] + ".json")


_SCALE_SEP = " @"               # 本地占位：注解名 + " @WxH:crop"，submit 时换成 LoadImage + ImageScale


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RemoteIndex:
    """本地文件内容 sha256 → ComfyUI 输出目录里的同一文件 [{instance, base, gpu, subfolder, filename, type}]"""

    MAX_PER_SHA = 4

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict | None = None if path else {}

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def add(self, sha: str, rec: dict):
        with self._lock:
            d = self._load()
            recs = [r for r in d.get(sha, []) if r.get("instance") != rec.get("instance")]
            d[sha] = ([rec] + recs)[: self.MAX_PER_SHA]
            self._save()

    def find(self, sha: str, instance: str) -> dict | None:
        with self._lock:
            return next((dict(r) for r in self._load().get(sha, []) if r.get("instance") == instance), None)

    def forget(self, sha: str, instance: str):
        with self._lock:
            d = self._load()
            if sha in d:
                d[sha] = [r for r in d[sha] if r.get("instance") != instance]
                if not d[sha]:
                    del d[sha]
                self._save()


def _default_index_path() -> Path:
    if os.environ.get("NS_COMFY_REMOTE_INDEX"):
        return Path(os.environ["NS_COMFY_REMOTE_INDEX"])
    root = Path(os.environ.get("NS_PROJECT_ROOT") or Path(__file__).resolve().parents[3])
    return root / "data" / "cache" / "comfy_remote_outputs.json"


remote_index = RemoteIndex(_default_index_path())   # 测试换成 RemoteIndex(tmp) / RemoteIndex(None)


def annotated_name(rec: dict) -> str:
    sub = rec.get("subfolder") or ""
    return (f"{sub}/{rec['filename']}" if sub else rec["filename"]) + OUTPUT_TAG


def _upload_payload(path: Path) -> tuple[bytes, str, str]:
    """→ (要传的字节, 文件名, 旧名 = 原内容 sha16 + 原后缀)。不透明的 PNG/WebP/BMP 转 JPEG q95；
    带透明 / 灰度 / 已是 JPEG 的原样传"""
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    suffix = Path(path).suffix.lower()
    legacy = digest + suffix
    if suffix in (".png", ".webp", ".bmp"):
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            opaque = im.mode == "RGB" or (im.mode == "RGBA" and im.getchannel("A").getextrema() == (255, 255))
            if opaque:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=UPLOAD_JPEG_QUALITY)
                return buf.getvalue(), digest + ".jpg", legacy
        except Exception:  # noqa: BLE001  打不开就原样传
            pass
    return data, legacy, legacy


def remote_info(client, path: Path) -> dict | None:
    """take.remote：这张本地图在 gpu3 哪个 ComfyUI 的输出目录里叫什么（+ sha256）；假客户端 / 没记录 → None"""
    fn = getattr(client, "remote_info", None)
    try:
        return fn(path) if fn else None
    except Exception:  # noqa: BLE001
        return None


def input_image(client, path: Path, subfolder: str = "ns_final/refs", *, size: tuple[int, int] | None = None,
                crop: str = "disabled", prepare=None) -> str:
    """LoadImage 用的图片名。path = 原图（多半是之前从 ComfyUI 下载的输出）；size / crop = 要的尺寸，
    prepare() = 本地做同样缩放后的文件（要上传时才调用）。真 ComfyClient：同实例输出目录已有 → [output] 注解
    + 图内 ImageScale；假客户端（测试）没有 input_image 方法 → 直接 upload_image(prepare())。"""
    if hasattr(client, "input_image"):
        return client.input_image(path, subfolder, size=size, crop=crop, prepare=prepare)
    return client.upload_image(prepare() if prepare else Path(path), subfolder)


class ComfyClient:
    # GPU 任务归属标签（K-OPS-G01，ns_agent.comfy_tag.make 的结果）：调用方设了就随 POST /prompt 放进 extra_data.ns_tag
    # （GPU 面板读它显示“谁在做什么”）；None = 不带。client_id 不变。
    ns_tag: dict | None = None

    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: float = 60, gpu: int | None = None,
                 upload_timeout: float = UPLOAD_TIMEOUT_S):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.gpu = gpu
        self.server_tag: str | None = None         # G36：非 gpu33 的服务器名（实例标识带上它，输出目录不跨服务器互通）
        self.client_id = uuid.uuid4().hex
        self._object_info: dict | None = None
        self._instance: str | None = None
        self._remote: dict[str, dict] = {}     # 本客户端发出的 [output] 名 → {sha, path, subfolder, size, crop, prepare}
        self.reuploads: list[str] = []         # 因 gpu3 上文件缺失而改为上传的（日志 / 测试用）

    def _request(self, path: str, data: bytes | None = None, headers: dict | None = None,
                 timeout: float | None = None, method: str | None = None) -> bytes:
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            err = ComfyError(f"{path}: HTTP {e.code} {body[:2000].decode('utf-8', 'replace')}")
            err.status, err.body = e.code, body
            raise err from e
        except urllib.error.URLError as e:
            raise ComfyUnavailable(f"{path}: {e.reason}") from e
        except (ConnectionError, TimeoutError, http.client.HTTPException) as e:
            # http.client.IncompleteRead / RemoteDisconnected = 传到一半连接断了，属于连接问题，不是图有问题（K-OPS-E02）
            raise ComfyUnavailable(f"{path}: {type(e).__name__}: {e}") from e

    def _json(self, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else None
        return json.loads(self._request(path, data, headers))

    def system_stats(self) -> dict:
        return self._json("/system_stats")

    def object_info(self, fresh: bool = False) -> dict:
        if self._object_info is not None and not fresh:
            return self._object_info
        now = time.time()
        with _OBJECT_INFO_LOCK:
            hit = _OBJECT_INFO_MEM.get(self.base)
            if hit and not fresh and now - hit[0] < OBJECT_INFO_TTL_S:
                self._object_info = hit[1]
                return self._object_info
            disk = _object_info_disk(self.base)
            if not fresh:
                try:
                    if now - disk.stat().st_mtime < OBJECT_INFO_TTL_S:
                        info = json.loads(disk.read_text(encoding="utf-8"))
                        _OBJECT_INFO_MEM[self.base] = (disk.stat().st_mtime, info)
                        self._object_info = info
                        return info
                except (OSError, ValueError):
                    pass
        info = self._json("/object_info")
        with _OBJECT_INFO_LOCK:
            _OBJECT_INFO_MEM[self.base] = (time.time(), info)
            try:
                disk = _object_info_disk(self.base)
                disk.parent.mkdir(parents=True, exist_ok=True)
                tmp = disk.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(info), encoding="utf-8")
                os.replace(tmp, disk)
            except OSError:
                pass
        self._object_info = info
        return info

    def check(self, workflow: dict) -> list[str]:
        """提交前检查：节点是否存在、模型文件是否在 ComfyUI 的列表里"""
        problems = self._check_with(self.object_info(), workflow)
        if problems:
            fresh = self.object_info(fresh=True)        # 缓存可能旧（新装节点 / 模型）：重拉一次再判
            problems = self._check_with(fresh, workflow)
        return problems

    def _check_with(self, info: dict, workflow: dict) -> list[str]:
        problems = []
        for nid, node in workflow.items():
            cls = node["class_type"]
            if cls not in info:
                problems.append(f"节点 {nid}: 没有 {cls}")
                continue
            spec = {**info[cls]["input"].get("required", {}), **info[cls]["input"].get("optional", {})}
            for key in MODEL_INPUTS:
                value = node["inputs"].get(key)
                if value is None or key not in spec:
                    continue
                options = spec[key][0]
                if isinstance(options, list) and value not in options:
                    problems.append(f"节点 {nid} ({cls}): 找不到 {key}={value}")
        return problems

    # ------------------------------------------------------------ gpu3 上已有的输出直接引用（fredw-92/F1）

    def instance(self) -> str:
        """实例标识：--output-directory（两个实例目录相同 → 同一标识，[output] 注解互通）；读不到就用地址"""
        if self._instance is None:
            key = None
            try:
                argv = (self.system_stats().get("system") or {}).get("argv") or []
                if "--output-directory" in argv:
                    key = "out:" + str(argv[argv.index("--output-directory") + 1]).rstrip("/")
                else:
                    key = "out-default:" + self.base
            except (ComfyError, ValueError, IndexError, AttributeError, TypeError):
                pass
            pre = f"{self.server_tag}|" if getattr(self, "server_tag", None) else ""
            if key is None:
                return pre + "url:" + self.base      # 暂时连不上：不缓存，下次再问
            self._instance = pre + key
        return self._instance

    def remote_info(self, path: Path) -> dict | None:
        """本地文件（下载的输出原样）在本实例输出目录里的记录 + sha256，写进 take.remote；没有 → None"""
        try:
            sha = file_sha256(path)
        except OSError:
            return None
        rec = remote_index.find(sha, self.instance())
        if rec:
            rec["sha256"] = sha
        return rec

    def input_image(self, path: Path, subfolder: str = "ns_final/refs", *, size=None, crop: str = "disabled",
                    prepare=None) -> str:
        path = Path(path)
        try:
            sha = file_sha256(path)
        except OSError:
            sha = None
        rec = remote_index.find(sha, self.instance()) if sha else None
        if rec:
            name = annotated_name(rec)
            if size:
                name += f"{_SCALE_SEP}{int(size[0])}x{int(size[1])}:{crop}"
            self._remote[name] = {"sha": sha, "path": path, "subfolder": subfolder, "size": size, "crop": crop,
                                  "prepare": prepare}
            return name
        return self.upload_image(prepare() if prepare else path, subfolder, remote=False)

    def upload_image(self, path: Path, subfolder: str = "ns_final", remote: bool = True) -> str:
        """同实例输出目录里已有同内容文件 → 返回 [output] 注解名；否则上传（JPEG q95、同内容跳过、≥900 s）"""
        if remote:
            return self.input_image(path, subfolder)
        data, name, legacy = _upload_payload(Path(path))
        for n in dict.fromkeys((name, legacy)):
            if self._input_exists(subfolder, n):
                return f"{subfolder}/{n}" if subfolder else n
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in (("subfolder", subfolder), ("overwrite", "true")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        res = json.loads(self._request("/upload/image", b"".join(parts),
                                       {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                       timeout=max(self.timeout, self.upload_timeout)))
        return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]

    def _input_exists(self, subfolder: str, name: str) -> bool:
        """输入目录（两个实例共用）里已有这个名字 → 不再上传（名字 = 原内容 sha，同名即同内容）"""
        q = urllib.parse.urlencode({"filename": name, "subfolder": subfolder, "type": "input"})
        try:
            self._request(f"/view?{q}", method="HEAD")
            return True
        except ComfyError:
            return False

    def _materialize(self, workflow: dict, swap: dict | None = None) -> dict:
        """本地占位名 → 真名：[output] 注解（要缩放的另接 ImageScale）；swap 里的（文件缺失改上传的）换成上传名"""
        wf = json.loads(json.dumps(workflow))
        swap = swap or {}
        for nid, node in list(wf.items()):
            if node.get("class_type") != "LoadImage":
                continue
            name = node["inputs"].get("image")
            if not isinstance(name, str):
                continue
            if name in swap:
                node["inputs"]["image"] = swap[name]
                continue
            spec = self._remote.get(name)
            if spec is None:
                continue
            node["inputs"]["image"] = name.split(_SCALE_SEP)[0]
            if spec["size"]:
                sid = f"{nid}_f1scale"
                for other in wf.values():
                    for k, v in other["inputs"].items():
                        if v == [nid, 0]:
                            other["inputs"][k] = [sid, 0]
                wf[sid] = {"class_type": "ImageScale",
                           "inputs": {"image": [nid, 0], "upscale_method": "lanczos", "width": int(spec["size"][0]),
                                      "height": int(spec["size"][1]), "crop": spec["crop"]}}
        return wf

    def _missing_remote(self, workflow: dict, node_errors: dict) -> list[str]:
        """node_errors 里 LoadImage 读不到的、且是本客户端发出的 [output] 名"""
        out = []
        for nid, err in (node_errors or {}).items():
            node = workflow.get(nid) or {}
            name = (node.get("inputs") or {}).get("image")
            if node.get("class_type") == "LoadImage" and name in self._remote and "image" in json.dumps(err):
                out.append(name)
        return out

    def _yield_peer(self) -> None:
        """LOCALGPU（11-01）：同卡轮流用的道（render_endpoints lanes 里配了 yield_url，如 laptop:0 → 本机看图服务 /yield）：
        交图之前先请对方卸载（推理中则等这条推理完，≤ yield_timeout_s），否则 ComfyUI 一加载模型就 OOM 崩（11-01 18:28 实测）。
        对方没起（连不上）→ 照常提交（对方没占卡）；对方在但超时没让出 → 不提交，抛 ComfyUnavailable（基础设施，调用方退避重试）
        ——11-01 18:52 实测：超时后照交，两边同卡挤进共享显存，出图和看图都慢到不动"""
        try:
            from . import endpoints as EP
            rec = next((v for v in EP._extra_lanes().values() if str(v.get("url", "")).rstrip("/") == self.base), None)
        except Exception:  # noqa: BLE001
            return
        url = (rec or {}).get("yield_url")
        if not url:
            return
        req = urllib.request.Request(url, data=json.dumps({"by": "ComfyClient " + self.base}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=float(rec.get("yield_timeout_s") or 300)).read()
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ConnectionRefusedError) or "refused" in str(e).lower() \
                    or "10061" in str(e):
                return                                   # 看图服务没起 = 没占卡
            raise ComfyUnavailable(f"同卡看图服务没让出（{url}）：{e}，未提交，稍后重试") from e
        except (TimeoutError, OSError) as e:
            raise ComfyUnavailable(f"同卡看图服务 {rec.get('yield_timeout_s') or 300}s 内没让出（推理中），未提交，稍后重试：{e}") from e

    def _post_prompt(self, wf: dict) -> dict:
        self._yield_peer()
        try:
            body = {"prompt": wf, "client_id": self.client_id}
            tag = getattr(self, "ns_tag", None)
            if isinstance(tag, dict) and tag:
                body["extra_data"] = {"ns_tag": dict(tag)}
            return self._json("/prompt", body)
        except ComfyUnavailable:
            raise
        except ComfyError as e:
            if getattr(e, "status", None) == 400:
                try:
                    return {"_http400": json.loads(e.body), "_msg": str(e)}
                except (TypeError, ValueError):
                    pass
            raise

    def submit(self, workflow: dict) -> str:
        swap: dict = {}
        for attempt in range(2):
            res = self._post_prompt(self._materialize(workflow, swap))
            body = res.get("_http400") or res
            errs = body.get("node_errors") or {}
            missing = self._missing_remote(workflow, errs) if attempt == 0 else []
            if missing:
                # gpu3 输出目录里那张图没了（被清理 / 换了目录）→ 忘掉记录，改为上传后重交一次
                if res.get("prompt_id"):
                    try:
                        self._json("/queue", {"delete": [res["prompt_id"]]})
                    except ComfyError:
                        pass
                for name in missing:
                    spec = self._remote[name]
                    remote_index.forget(spec["sha"], self.instance())
                    src = spec["prepare"]() if spec["prepare"] else spec["path"]
                    swap[name] = self.upload_image(Path(src), spec["subfolder"], remote=False)
                    self.reuploads.append(name)
                continue
            if res.get("_http400"):
                raise ComfyError(res["_msg"])
            if errs:
                raise ComfyError(f"node_errors: {json.dumps(errs, ensure_ascii=False)[:2000]}")
            return res["prompt_id"]
        raise ComfyError("提交失败")

    def wait(self, prompt_id: str, timeout_s: float, poll_s: float = 3) -> dict:
        deadline = time.monotonic() + timeout_s
        fail_since = None
        while time.monotonic() < deadline:
            try:
                hist = self._json(f"/history/{prompt_id}")
                fail_since = None
            except ComfyUnavailable:
                # K-OPS-E01：作业已经交上去了，一次轮询超时 / 断连不等于作业没了——在宽限期内继续等，不重派、不重渲染
                fail_since = fail_since or time.monotonic()
                if time.monotonic() - fail_since > POLL_GRACE_S:
                    raise
                time.sleep(min(15.0, max(poll_s, 5.0)))
                continue
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[0][1].get("exception_message", "") if msgs else ""
                    raise ComfyError(f"执行失败: {detail.strip()[:1000]}")
                if status.get("completed"):
                    return entry
            time.sleep(poll_s)
        self.cancel(prompt_id)
        raise ComfyError(f"超时 {timeout_s}s")

    def interrupt(self, prompt_id: str | None = None):
        """K-OPS-E03：给了 prompt_id 就只打断这一单（ComfyUI 0.42 /interrupt 支持 prompt_id，不是它在跑就不动）；
        不带 = 全局打断，会打断这张卡上别人正在跑的作业，只给明确要清卡的调用方用"""
        body = json.dumps({"prompt_id": prompt_id}).encode() if prompt_id else b"{}"
        try:
            self._request("/interrupt", body, {"Content-Type": "application/json"})
        except ComfyError:
            pass

    def cancel(self, prompt_id: str):
        """只取消自己这一单：排队中 → 出队；正在跑 → 定向 interrupt。别人的作业不动"""
        try:
            self._json("/queue", {"delete": [prompt_id]})
        except ComfyError:
            pass
        self.interrupt(prompt_id)

    def free(self, unload_models: bool = True):
        self._json("/free", {"unload_models": unload_models, "free_memory": True})

    @staticmethod
    def output_files(entry: dict) -> list[dict]:
        files = []
        for out in entry.get("outputs", {}).values():
            for items in out.values():
                if isinstance(items, list):
                    files += [i for i in items if isinstance(i, dict) and "filename" in i]
        return files

    def download(self, item: dict, dest: Path) -> Path:
        q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                    "type": item.get("type", "output")})
        dest.parent.mkdir(parents=True, exist_ok=True)
        for k in range(max(1, DOWNLOAD_TRIES)):          # K-OPS-E02：传到一半断了就重下，图已经在服务器上，不重渲染
            try:
                data = self._request(f"/view?{q}", timeout=max(self.timeout, 300))
                break
            except ComfyUnavailable:
                if k + 1 >= max(1, DOWNLOAD_TRIES):
                    raise
                time.sleep(5 * (k + 1))
        dest.write_bytes(data)
        if item.get("type", "output") == "output":
            # 记下 gpu3 上的名字：之后同实例要用这张图时直接 [output] 引用，不再上传
            try:
                remote_index.add(hashlib.sha256(data).hexdigest(), {
                    "instance": self.instance(), "base": self.base, "gpu": self.gpu,
                    "subfolder": item.get("subfolder", ""), "filename": item["filename"], "type": "output",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:  # noqa: BLE001  记不上只是下次要上传
                pass
        return dest

    def run(self, workflow: dict, timeout_s: float) -> tuple[dict, float]:
        """检查 → 提交 → 等待；返回 (history 条目, 耗时秒)"""
        problems = self.check(workflow)
        if problems:
            raise ComfyError("工作流检查不通过:\n  " + "\n  ".join(problems))
        t0 = time.monotonic()
        entry = self.wait(self.submit(workflow), timeout_s)
        return entry, time.monotonic() - t0
