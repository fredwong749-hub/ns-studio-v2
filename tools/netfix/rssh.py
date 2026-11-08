"""远程命令闸门（NETFIX P0 11-02，总调度 fredw-2b 派；规则 K-OPS-N07）。

项目方 00:0x 原话：“服务器连接的问题还蛮严重的，一直在断……找一个更 stable 的方法……这个问题是 P0”。
根因（总调度 00:07 实测）：笔记本上 26 个 ssh.exe，到跳板 ESTABLISHED 13 / TIME_WAIT 12——十几个后台程序各自每次新建 ssh，
每次要 6–7 s 握手；跳板 sshd MaxStartups 默认 10:30:100，同时握手 > 10 就随机丢 → Connection reset / banner timeout。

做法：远程命令 / 文件传输一律复用 WSL OpenSSH 的常驻主连接（ControlMaster，由 tunnel_supervisor 的 gpu33-cmd / gpu36-cmd 托着）：
  wsl.exe -d Ubuntu-24.04 --exec ssh -S <sock> -o ControlMaster=no -o ProxyCommand=false <host> <命令>
ProxyCommand=false = 只许复用：主连接不在时 0.1 s 内失败，绝不新建握手（11-02 00:2x 实测）。
  · rssh.run(host, cmd, input=..., timeout=...)：显式调用（新代码用这个）
  · rewrite(argv)：把一条 ["ssh", ..., host, cmd...] 改写成上面的形式；venv 的 sitecustomize 用它拦截所有 subprocess 起的 ssh
    （只拦 ssh 到 gpu33 / gpu36、且不带 -L/-R/-D/-W/-N/-M/-f/-S/-O 的命令调用；scp 不拦；别的不碰）
安全阀：环境变量 NS_SSH_SHIM=off → 一律不改写（直接调 ssh）。监管进程心跳过期（> 90 s）也不改写（退回直接 ssh，免得全断）。
服务器上的 venv 不装 shim（只在笔记本）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATUS = ROOT / "data/netfix/net_status.json"
WSL = ["C:/Windows/System32/wsl.exe", "-d", "Ubuntu-24.04", "--exec"]
SOCK = {"gpu33": "/home/fredw/.ssh/nsnf/gpu33-cmd.sock", "gpu36": "/home/fredw/.ssh/nsnf/gpu36-cmd.sock"}
POOL = {h: [f"/home/fredw/.ssh/nsnf/{h}-{k}.sock" for k in ("cmd", "cmd2", "cmd3", "cmd4")] for h in SOCK}   # 每台 4 条（服务器 MaxSessions 10）


def pick_sock(host: str) -> str:
    """在监管进程报“up”的主连接里随机挑一条分摊会话；读不到状态就用第一条"""
    import random
    try:
        hs = json.loads(STATUS.read_text(encoding="utf-8")).get("hosts") or {}
        up = [hs[k]["sock"] if "sock" in hs[k] else f"/home/fredw/.ssh/nsnf/{k}.sock" for k in hs
              if k.startswith(host + "-cmd") and hs[k].get("state") == "up"]
    except (OSError, ValueError, KeyError):
        up = []
    return random.choice(up) if up else SOCK[host]
ARG_OPTS = set("bcDEeFIiJLlmOoPpQRSWwB")          # 带参数的 ssh 选项
SKIP_OPTS = set("LRDWNMfSO")                        # 出现就不改写（转发 / 主连接 / 控制命令）
DROP_O = ("controlmaster", "controlpath", "controlpersist", "proxyjump", "proxycommand", "identityfile", "identitiesonly",
          "userknownhostsfile", "stricthostkeychecking", "hostkeyalias")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def enabled() -> bool:
    if os.environ.get("NS_SSH_SHIM", "").lower() in ("off", "0", "no"):
        return False
    try:
        return time.time() - STATUS.stat().st_mtime < 90       # 监管进程在岗（主连接由它托着）
    except OSError:
        return False


def rewrite(argv) -> list[str] | None:
    """→ 改写后的 argv；不该拦的返回 None"""
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    exe = os.path.basename(str(argv[0])).lower()
    if exe not in ("ssh", "ssh.exe"):
        return None
    kept: list[str] = []
    i, host = 1, None
    a = [str(x) for x in argv]
    while i < len(a):
        x = a[i]
        if x == "--":
            i += 1
            break
        if x.startswith("-") and len(x) > 1:
            flags = x[1:]
            for j, ch in enumerate(flags):
                if ch in SKIP_OPTS:
                    return None
                if ch in ARG_OPTS:
                    val = flags[j + 1:] or (a[i + 1] if i + 1 < len(a) else "")
                    if not flags[j + 1:]:
                        i += 1
                    if ch == "o":
                        if not val.split("=", 1)[0].strip().lower() in DROP_O:
                            kept += ["-o", val]
                    elif ch in "lp":                # 用户 / 端口对复用无意义但保留无害：丢掉，免得和主连接不一致
                        pass
                    # -i / -F / -J / 其它带参选项：主连接已认证，丢弃
                    break
                kept.append("-" + ch)               # 无参数标志（-T -n -q -t -v -4 -C ...）
            i += 1
            continue
        host = x
        i += 1
        break
    if host is None:
        return None
    if "@" in host:
        user, h = host.split("@", 1)
        if user != "root":
            return None
        host = h
    if host not in SOCK:
        return None
    rest = a[i:]
    if not rest:                                     # 交互式登录不拦
        return None
    rest = [with_tz(rest[0]), *rest[1:]]
    return WSL + ["ssh", "-S", pick_sock(host), "-o", "ControlMaster=no", "-o", "ProxyCommand=false", "-o", "BatchMode=yes",
                  *kept, host, *rest]


TZ_NAME = "Asia/Tokyo"
TZ_PREFIX = f"TZ={TZ_NAME} && export TZ && "


def with_tz(cmd: str) -> str:
    """时区根治（DEBUG-RSI tz_check PATCH_RSSH）：服务器系统时区 America/New_York、笔记本 UTC+9，远程命令写的时间戳要和笔记本一致 →
    一律前缀 'TZ=Asia/Tokyo && export TZ && '（不用 'TZ=… cmd'：那只作用于第一条命令，'cd x && y' 里的 y 拿不到）。已带就不重复加"""
    return cmd if cmd.lstrip().startswith("TZ=") else TZ_PREFIX + cmd


def run(host: str, cmd: str, input: bytes | None = None, timeout: float = 900, check: bool = False) -> subprocess.CompletedProcess:
    """显式复用主连接执行远程命令（主连接不在 → 立刻失败 rc=255，不新建握手）。
    14:2x：挑中的主连接恰好在重建（socket 不在 / mux 拒绝）→ 立刻换同台另一条主连接再试（不等待、不新握手），
    每次调用的结果记一行 data/netfix/rssh_calls.jsonl（监管进程汇总成 net_status.rssh 成功率）"""
    t0 = time.time()
    argv = rewrite(["ssh", host, cmd]) if enabled() else None
    if argv and os.name != "nt" and argv[:len(WSL)] == WSL:
        argv = argv[len(WSL):]      # W45：笔记本 WSL 里直接调（已经在 WSL 内，不再套 wsl.exe），同一批主连接 socket
    tried = []
    if argv:
        socks = [argv[argv.index("-S") + 1]] + [s for s in POOL.get(host, []) if s != argv[argv.index("-S") + 1]]
        for sk in socks:
            argv[argv.index("-S") + 1] = sk
            tried.append(sk.rsplit("/", 1)[-1])
            try:
                res = subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=False, creationflags=NOWIN)
            except subprocess.TimeoutExpired:
                _note(host, 124, time.time() - t0, tried, cmd)
                raise
            err = (res.stderr or b"")[-300:]
            mux_down = res.returncode == 255 and (b"Control socket connect" in err or b"mux_client" in err
                                                  or b"Connection closed by UNKNOWN" in err)
            if not mux_down:
                break
    else:
        try:
            res = subprocess.run(["ssh", "-o", "BatchMode=yes", host, with_tz(cmd)], input=input, capture_output=True,
                                 timeout=timeout, check=False, creationflags=NOWIN)
        except subprocess.TimeoutExpired:
            _note(host, 124, time.time() - t0, ["direct"], cmd)
            raise
    _note(host, res.returncode, time.time() - t0, tried or ["direct"], cmd)
    if check and res.returncode:
        raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)
    return res


CALLS = ROOT / "data/netfix/rssh_calls.jsonl"


def _note(host: str, rc: int, dur: float, tried: list, cmd: str = "") -> None:
    """一次调用一行（几十字节，追加写）；“成功” = 连接层没出错（rc != 255 且没超时），远端命令自己的非零退出码不算通道失败"""
    try:
        with open(CALLS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "host": host, "rc": rc, "ok": rc not in (255, 124),
                                "dur": round(dur, 2), "tried": tried, "cmd": (cmd or "")[:40]}) + "\n")   # 命令前 40 字：定位哪类调用超时
    except OSError:   # arch-ok: 指标落盘失败不影响调用本身（只少一条统计）
        pass


def _local_self(host: str) -> str | None:
    """服务器上（OFFLINE_SELF 有值）：目标是本机 → 'self'；是另一台 → 'other'（两台之间按设计不配 ssh 互信）；笔记本上 → None"""
    me = os.environ.get("OFFLINE_SELF")
    if not me:
        return None
    h = host.split("@", 1)[-1]
    # OFFLINE_ALIASES（resident_restart.sh：gpu36 上 = gpu33）：契约打分代码仍写着 full_score("gpu33")，在 gpu36 上要当本机执行
    # （FACTORY 11-02 09:0x P0：漏了这条，sv-06 起 gpu36 每单打分都被判“跨机”→ 退回 producer venv 的 CPU 打分 → 无 torch → score_infra）
    aliases = {a.strip() for a in os.environ.get("OFFLINE_ALIASES", "").split(",") if a.strip()}
    return "self" if h in (me, f"{me}-t", f"{me}-tb", "localhost", "127.0.0.1", *aliases) else "other"


def run_retry(host: str, cmd: str, input: bytes | str | None = None, timeout: float = 900, tries: int = 4,
              backoff_s: float = 10, text: bool = False, retries: int | None = None) -> subprocess.CompletedProcess:
    """统一远程命令接口（SYSARCH 4-1：各文件的 def ssh 换成一行调用这个；K-OPS-N07）。
    · 笔记本：经监管进程托管的常驻主连接（rssh.run，不新建跳板握手）；
    · 服务器（OFFLINE_SELF）：目标是本机 → 本机 bash -c 直接执行；目标是另一台 → 立刻返回 rc=255（服务器之间无互信，设计如此）。
    重试：rc == 255（连接层失败）或超时 → 等 backoff_s × 第几次 再试，最多 tries 次（retries=N 等价 tries=N+1）；
    其它非零退出码 = 远端命令自己的结果，直接返回不重试。退避下限 10 s（不许更快：快速重连在跳板看来像暴力破解）。
    超时用尽 → 返回 rc=124、stderr 写明超时。text=True → stdout / stderr 按 utf-8（replace）解码成 str。"""
    if retries is not None:
        tries = int(retries) + 1
    tries, backoff_s = max(1, int(tries)), max(10.0, float(backoff_s))
    data = input.encode("utf-8") if isinstance(input, str) else input
    where = _local_self(host)
    res = None
    for k in range(1, tries + 1):
        try:
            if where == "other":
                res = subprocess.CompletedProcess(["ssh", host, cmd], 255, b"",
                                                  "服务器之间无 ssh 互信（设计如此），跨机请走 HTTP（如 10.0.0.36:8870）".encode("utf-8"))
                break
            if where == "self":
                res = subprocess.run(["bash", "-c", with_tz(cmd)], input=data, capture_output=True, timeout=timeout)
            else:
                res = run(host, cmd, input=data, timeout=timeout)
        except subprocess.TimeoutExpired:
            res = subprocess.CompletedProcess(["ssh", host, cmd], 124, b"", f"rssh.run_retry: 超时 {timeout}s（第 {k}/{tries} 次）".encode("utf-8"))
            if k < tries:
                time.sleep(backoff_s * k)
            continue
        if res.returncode != 255 or k == tries:
            break
        time.sleep(backoff_s * k)
    if text:
        res = subprocess.CompletedProcess(res.args, res.returncode, (res.stdout or b"").decode("utf-8", "replace"),
                                          (res.stderr or b"").decode("utf-8", "replace"))
    return res


GIT_BIN = "C:/Program Files/Git/usr/bin"


def bulk_put(host: str, files: list, remote_dir: str, timeout: float = 600) -> subprocess.CompletedProcess | None:
    """W21 NETFIX-BULK（运维 FONG-QUICK：大文件不要走 -t，用 gpu36-hk / gpu33-hk 单独传）：
    Git sftp 经 <host>-hk 单独一条连接（hkjump → 194relay → 服务器），不占命令主连接 / 底座 base-a。
    files = [(本机路径, 远端文件名)] → 全部 put 进 remote_dir（不存在就建）。
    只在笔记本（Windows、非 OFFLINE_SELF）且 NS_BULK_UPLOAD != off 时做；否则返回 None（调用方退回原路径）。
    连接 / 传输失败 → rc != 0（调用方退回原路径，不当成功）。"""
    if os.name != "nt" or os.environ.get("OFFLINE_SELF") or os.environ.get("NS_BULK_UPLOAD", "").lower() in ("off", "0", "no"):
        return None
    h = host.split("@", 1)[-1]
    if h not in SOCK:
        return None
    sftp, ssh = f"{GIT_BIN}/sftp.exe", f"{GIT_BIN}/ssh.exe"
    if not Path(sftp).exists():
        return None
    q = lambda x: '"' + str(x).replace("\\", "/").replace('"', '') + '"'
    parent = remote_dir.rstrip("/").rsplit("/", 1)[0]
    lines = [f"-mkdir {q(parent)}", f"-mkdir {q(remote_dir)}"] + [f"put {q(Path(lp).resolve())} {q(remote_dir.rstrip('/') + '/' + rn)}" for lp, rn in files]
    t0 = time.time()
    try:
        res = subprocess.run([sftp, "-F", "config/netfix/ssh_config", "-S", ssh, "-o", "BatchMode=yes", "-b", "-", f"{h}-hk"],
                             input=("\n".join(lines) + "\n").encode("utf-8"), capture_output=True, timeout=timeout, cwd=str(ROOT),
                             creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        _bulk_note(h, 124, time.time() - t0, len(files))
        return subprocess.CompletedProcess(["sftp", h], 124, b"", f"bulk_put 超时 {timeout}s".encode("utf-8"))
    except OSError as e:
        return subprocess.CompletedProcess(["sftp", h], 255, b"", repr(e).encode("utf-8"))
    _bulk_note(h, res.returncode, time.time() - t0, len(files))
    return res


BULK_CALLS = ROOT / "data/netfix/bulk_calls.jsonl"


def _bulk_note(host: str, rc: int, dur: float, n: int) -> None:
    """独立传输一次一行（不混进 rssh_calls.jsonl：那里统计的是命令主连接成功率）"""
    try:
        with open(BULK_CALLS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "host": host, "rc": rc, "ok": rc == 0, "dur": round(dur, 2), "files": n,
                                "via": f"{host}-hk"}) + "\n")
    except OSError:   # arch-ok: 指标落盘失败不影响传输本身
        pass


def status() -> dict:
    try:
        d = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {h: (d.get("hosts") or {}).get(f"{h}-cmd", {}).get("state") for h in SOCK}
