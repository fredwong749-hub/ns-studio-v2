"""FACTORY D1：唯一车道表 config/factory/hosts.json 的唯一读取入口（SYSARCH 4-2 / ARCH_ROADMAP §8，总调度 11-02 定）。
读不到 / 没登记 → raise（失败关闭，K-OPS-F09 同口径）。服务器身份 = NS_FACTORY_HOST 或 OFFLINE_SELF（K-OPS-Y02 服务器环境契约）。

  lane_url(lane, from_host=None)  服务器本机 → http://127.0.0.1:<comfy_port>；笔记本 → http://127.0.0.1:<tunnel_port>
  lanes(host=None, role=None, enabled=True)   comfy_output(lane)   server_root(host)   aliases(host)   python(host)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("NS_ROOT") or Path(__file__).resolve().parents[2]).resolve()
HOSTS_JSON = ROOT / "config/factory/hosts.json"


class HostsError(RuntimeError):
    """车道表读不到 / 没登记（失败关闭）"""


def load(path: Path | None = None) -> dict:
    p = path or HOSTS_JSON
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HostsError(f"车道表读不出：{p}（{e}）") from e
    if d.get("schema") != "nsfs.factory.hosts/1" or not isinstance(d.get("hosts"), dict):
        raise HostsError(f"车道表格式不对：{p}")
    return d


def self_host() -> str | None:
    return os.environ.get("NS_FACTORY_HOST") or os.environ.get("OFFLINE_SELF") or None


def resolve_self(hostname: str | None = None, d: dict | None = None) -> str | None:
    """机器真实主机名 → 车道表里的主机名（W-INFRA 11-06：gpu33 的 `hostname -s` 是 gpu3、gpu36 是 gpu6，
    review_instances.sh / review_lan_proxy.sh 原先拿 hostname 直接和 lane 比 → 在 gpu33 本机也判 WRONG_HOST）。
    顺序：NS_FACTORY_HOST / OFFLINE_SELF（环境契约优先）→ hosts.json 的 hostname / hostnames 字段 → 主机名本身就是表里的键 → None。
    不套 aliases（gpu36 的别名 gpu33 是“代跑 gpu33 的活”，不是“我是 gpu33”）"""
    me = self_host()
    if me:
        return me
    if hostname is None:
        import socket
        hostname = socket.gethostname()
    hn = str(hostname or "").split(".", 1)[0].strip().lower()
    if not hn:
        return None
    try:
        d = d or load()
    except HostsError:  # arch-ok: 读不到 hosts.json = 认不出本机，调用方（review_instances.sh / lan_proxy）退回 hostname -s
        return None
    hosts = d.get("hosts") or {}
    for name, rec in hosts.items():
        names = [rec.get("hostname")] + list(rec.get("hostnames") or [])
        if hn in {str(x).split(".", 1)[0].strip().lower() for x in names if x}:
            return name
    return hn if hn in hosts else None


def host_of(lane: str) -> str:
    return str(lane).split(":", 1)[0]


def _host(host: str, d: dict | None = None) -> dict:
    d = d or load()
    h = d["hosts"].get(host)
    if h is None:
        raise HostsError(f"车道表里没有主机 {host}")
    return h


def _lane(lane: str, d: dict | None = None) -> dict:
    d = d or load()
    ln = _host(host_of(lane), d)["lanes"].get(lane)
    if ln is None:
        raise HostsError(f"车道表里没有道 {lane}")
    return ln


def lanes(host: str | None = None, role: str | None = None, enabled: bool | None = True, d: dict | None = None) -> dict:
    """{lane: 记录}；host=None = 全部主机"""
    d = d or load()
    out = {}
    for hn, h in d["hosts"].items():
        if host and hn != host:
            continue
        for ln, rec in (h.get("lanes") or {}).items():
            if enabled is not None and bool(rec.get("enabled")) != enabled:
                continue
            if role and role not in (rec.get("roles") or []):
                continue
            out[ln] = rec
    return out


def lane_url(lane: str, from_host: str | None = None, d: dict | None = None) -> str:
    """from_host 缺省 = 本进程身份：在道所在的服务器（或它的别名机）上 = 本机端口，否则（笔记本）= 隧道端口"""
    d = d or load()
    rec = _lane(lane, d)
    me = from_host if from_host is not None else self_host()
    h = host_of(lane)
    if me and (me == h or h in (_host(me, d).get("aliases") or [])):
        return f"http://127.0.0.1:{int(rec['comfy_port'])}"
    if me and me != "laptop":
        raise HostsError(f"{me} 不能直连 {lane}（服务器之间不互连，K-OPS-Y02）")
    tp = rec.get("tunnel_port")
    if not tp:
        if h == "laptop":
            return f"http://127.0.0.1:{int(rec['comfy_port'])}"
        raise HostsError(f"{lane} 没有隧道端口")
    return f"http://127.0.0.1:{int(tp)}"


def comfy_output(lane: str, d: dict | None = None) -> str:
    v = _lane(lane, d).get("comfy_output")
    if not v:
        raise HostsError(f"{lane} 没登记 comfy_output")
    return v


def server_root(host: str, d: dict | None = None) -> str:
    v = _host(host, d).get("repo")
    if not v:
        raise HostsError(f"{host} 没登记 repo")
    return v


def video_root(host: str | None = None, d: dict | None = None) -> str:
    """视频 worker（vidgen / night 远端命令 / 同步）的服务器代码根。PRODMERGE-O 11-03：= system_version 发版部署目录
    （hosts.json video_root，与 repo 相同；/root/work/fredw/video10_srv 退役）。host=None：所有登记了 video_root 的机器必须一致，否则 raise"""
    d = d or load()
    if host:
        v = _host(host, d).get("video_root")
        if not v:
            raise HostsError(f"{host} 没登记 video_root")
        return v
    vals = {h: v.get("video_root") for h, v in (d.get("hosts") or {}).items() if v.get("video_root")}
    if not vals or len(set(vals.values())) != 1:
        raise HostsError(f"各机器 video_root 不一致或没登记：{vals}")
    return next(iter(vals.values()))


def video_code_managed(host: str, d: dict | None = None) -> bool:
    """video_root 就是发版部署目录 → 代码只由 system_version deploy 下发，series10_video/deploy.push 只推作业 / 输入图 / 模型"""
    return video_root(host, d).rstrip("/") == server_root(host, d).rstrip("/")


def aliases(host: str, d: dict | None = None) -> list[str]:
    return list(_host(host, d).get("aliases") or [])


def exec_host(host: str, d: dict | None = None) -> str:
    """命令实际在哪台机器上跑（FIX-SCORE36 11-06）：服务器上目标是本机的别名（hosts.json aliases / env OFFLINE_ALIASES，
    与 rssh._local_self 同口径：gpu36 上 "gpu33" = 本机执行）→ 返回本机名；否则原样。
    按主机取的解释器 / 坏卡 / 仓库路径必须按这台取——原来 gpu36 上 full_score("gpu33") 拿 gpu33 的 comfy_python（gpu36 上没有这个文件）"""
    me = self_host()
    if not me or me == host or me == "laptop":
        return host
    env_al = {a.strip() for a in os.environ.get("OFFLINE_ALIASES", "").split(",") if a.strip()}
    try:
        al = set(aliases(me, d))
    except HostsError:
        al = set()
    return me if host in (al | env_al) else host


def broken_gpus(host: str, d: dict | None = None) -> list[str]:
    return [str(g) for g in (_host(host, d).get("broken_gpus") or [])]


def python(host: str, d: dict | None = None) -> str:
    v = _host(host, d).get("python")
    if not v:
        raise HostsError(f"{host} 没登记 python")
    return v


def recipe_max_frames(lane: str, recipe: str, d: dict | None = None) -> int | None:
    """某道上某配方的帧数上限（例：3090 上 H3 FunControlNet = 107）；没登记 = None（调用方用配方自己的默认）"""
    v = (_lane(lane, d).get("recipe_max_frames") or {}).get(recipe)
    return int(v) if v else None


def owner_controlled(host: str, d: dict | None = None) -> bool:
    """L-128：项目方自有机器（笔记本 + gpu33 + gpu36）。没写 / 不认识的机器 = False（失败关闭）"""
    try:
        return bool(_host(host, d).get("owner_controlled"))
    except Exception:  # noqa: BLE001  # arch-ok: 不认识的机器 = 不是自有机器（local_only 作业不许去）
        return False


def owner_lane(lane: str, d: dict | None = None) -> bool:
    """这条道是否在项目方自有机器上（route=local_only 的作业只许这些道，L-128）；没指定道 = False（失败关闭）"""
    if not lane:
        return False
    try:
        return owner_controlled(host_of(lane), d)
    except Exception:  # noqa: BLE001  # arch-ok: 认不出道 = 不放行
        return False


def main(argv=None) -> int:
    """命令行（服务器上 bash 脚本用，只依赖标准库）：
      python3 tools/factory/hosts.py self [主机名]   → 打印车道表主机名（gpu3 → gpu33）；认不出打印空行、退出码 1"""
    import sys
    a = list(sys.argv[1:] if argv is None else argv)
    if a and a[0] == "self":
        r = resolve_self(a[1] if len(a) > 1 else None)
        print(r or "")
        return 0 if r else 1
    print("usage: hosts.py self [hostname]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
