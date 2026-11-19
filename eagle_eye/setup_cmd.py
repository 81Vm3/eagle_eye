"""eagle setup [--check] [--version V] [--from-source] [--force]：准备 EAGLE_HOME，一步装好小红书渠道。

    1. 建 EAGLE_HOME 布局（bin/、xhs/、x/）
    2. 下载当前平台的 eagle-xhs Release 资产，按 checksums.txt 校验 sha256，chmod +x
       （或 --from-source：源码仓库 + go 时本地构建）；已是目标版本则跳过
    3. eagle-xhs init -config $EAGLE_HOME/xhs/config.yaml（已存在不覆盖）
    4. 打印下一步

--check 只体检、不改文件。约定见 docs/distribution.md。只用标准库（urllib）。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, paths

REPO = "81Vm3/eagle_eye"
RELEASE_URL = "https://github.com/" + REPO + "/releases/download/v{version}"
RELEASES_PAGE = "https://github.com/" + REPO + "/releases"
HTTP_TIMEOUT = 60


class SetupError(RuntimeError):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---- Release 资产 ----

def _run_logged(cmd, cwd=None):
    """运行子进程，stdout/stderr 都转到我们的 stderr（stdout 只放结果）；返回退出码。"""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        log(f"  {line}")
    return r.returncode


def asset_name(plat=None, machine=None):
    goos, goarch = paths.goos_goarch(plat, machine)
    return f"eagle-xhs-{goos}-{goarch}" + (".exe" if goos == "windows" else "")


def base_url(version):
    env = os.environ.get("EAGLE_XHS_BASE_URL")
    return (env or RELEASE_URL.format(version=version)).rstrip("/")


def parse_checksums(text):
    """sha256sum 格式：<hex>  <文件名>（二进制模式的 *文件名 也接受）。"""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def _open(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"eagle-eye-setup/{__version__}"})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)  # noqa: S310 — https/file（测试）均可


def _manual_hint(version, asset, dest):
    return (f"\n可手动下载 {RELEASE_URL.format(version=version)}/{asset}（校验和在同目录 checksums.txt；所有版本见 {RELEASES_PAGE}），"
            f"放到 {dest} 并 chmod +x；或在源码仓库里 eagle setup --from-source。")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def download_binary(version, dest, plat=None, machine=None):
    """下载并校验 eagle-xhs 到 dest（先写临时文件，校验通过才替换）。"""
    asset = asset_name(plat, machine)
    base = base_url(version)
    hint = _manual_hint(version, asset, dest)

    def fetch_text(url):
        try:
            with _open(url) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise SetupError(f"下载 {url} 失败: HTTP {e.code}（版本 v{version} 的 Release 不存在或资产缺失？）{hint}") from e
        except (urllib.error.URLError, OSError) as e:
            raise SetupError(f"下载 {url} 失败: {getattr(e, 'reason', e)}{hint}") from e

    sums = parse_checksums(fetch_text(f"{base}/checksums.txt"))
    expected = sums.get(asset)
    if not expected:
        raise SetupError(f"checksums.txt 里没有 {asset}（该版本不支持当前平台？）{hint}")

    dest = Path(dest)
    if dest.is_file() and file_sha256(dest) == expected:
        os.chmod(dest, 0o755)
        log(f"{dest} 与 Release 中的 {asset} 相同（sha256 一致），跳过下载")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".download")
    url = f"{base}/{asset}"
    log(f"下载 {url}")
    h = hashlib.sha256()
    try:
        with _open(url) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"下载 {url} 失败: HTTP {e.code}{hint}") from e
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"下载 {url} 失败: {getattr(e, 'reason', e)}{hint}") from e
    got = h.hexdigest()
    if got != expected:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"{asset} sha256 校验失败：期望 {expected}，实际 {got}（文件损坏或被篡改，已丢弃）{hint}")
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    log(f"已安装 {dest}（sha256 校验通过）")
    return dest


def build_from_source(version, dest):
    root = paths.source_root()
    if not root or not (root / "backends" / "xhs" / "go.mod").is_file():
        raise SetupError("--from-source 只能在源码仓库里运行（找不到 backends/xhs）")
    go = os.environ.get("GO") or shutil.which("go")
    if not go:
        raise SetupError("--from-source 需要 Go（PATH 里找不到 go，也可用环境变量 GO 指定）")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [go, "build", "-trimpath", "-ldflags", f"-s -w -X main.version={version}", "-o", str(dest), "."]
    log(f"本地构建: {' '.join(cmd)}（cwd=backends/xhs）")
    rc = _run_logged(cmd, cwd=root / "backends" / "xhs")
    if rc != 0:
        raise SetupError(f"go build 失败（exit {rc}）")
    return dest


def binary_version(binary):
    """eagle-xhs version 的输出（首行）；二进制不存在或不支持 version 时返回 None。"""
    binary = Path(binary)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    try:
        r = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or not lines:
        return None
    return lines[0].strip().removeprefix("eagle-xhs").strip().removeprefix("v")


def init_config(binary, config):
    config = Path(config)
    if config.exists():
        log(f"小红书配置已存在，不覆盖: {config}")
        return False
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        rc = _run_logged([str(binary), "init", "-config", str(config)])
    except OSError as e:
        raise SetupError(f"无法运行 {binary}: {e}") from e
    if rc != 0 or not config.exists():
        raise SetupError(f"eagle-xhs init 失败（exit {rc}）。二进制太旧时运行 eagle setup --force 重新下载")
    log(f"已生成小红书配置: {config}")
    return True


NEXT_STEPS = """\
eagle_eye 已就绪（EAGLE_HOME={home}）。下一步：

1. X（默认渠道）
   eagle x extension --open      # 打印并打开 cookie 同步扩展目录
   在 Chrome/Edge 扩展页（chrome://extensions）打开「开发者模式」→「加载已解压的扩展程序」→ 选上面的目录
   浏览器里登录 x.com，然后运行：eagle x login
2. 小红书（可选，慢、有封号风险，搜索时需显式 -c xhs）
   eagle xhs login               # 首次扫码登录（会弹出一次专用 Chrome 窗口，登录后自动关闭）
   eagle search -c xhs '关键词'   # 默认无头模式：后台启动 Chrome，不弹窗口
   配置在 {xhs_config}，需要时可改（eagle xhs browser 仅用于 headless: false 或调试）
3. 让你的 Agent 用上 eagle（任何能执行 shell 命令的 Agent 都行）
   eagle skill install           # 支持 Agent Skills 的工具：装到 ~/.agents/skills/eagle-search
                                 #   Claude Code 加 --agent claude；其他工具查阅其文档，用 --target <skills 目录>
   eagle skill show              # 不支持 Skill 的工具：把输出放进它的指令文件（如 AGENTS.md）或系统提示词
4. 体检：eagle setup --check；看渠道：eagle channels
"""


def run_setup(version=None, from_source=False, force=False, home=None):
    home = Path(home or paths.eagle_home())
    version = (version or __version__).removeprefix("v")
    for d in (home, home / "bin", home / "xhs", home / "x"):
        d.mkdir(parents=True, exist_ok=True)
    log(f"EAGLE_HOME: {home}")
    binary = paths.xhs_binary_path(home)
    current = binary_version(binary)
    if from_source:
        build_from_source(version, binary)
    elif current == version and not force:
        log(f"eagle-xhs 已是 {version}，跳过下载（--force 重新下载）")
    else:
        if current:
            log(f"eagle-xhs 当前版本 {current}，更新到 {version}")
        download_binary(version, binary)
    init_config(binary, paths.xhs_config_path(home))
    root = paths.source_root()
    if root and os.environ.get("EAGLE_CONFIG"):
        log(f"注意：当前 EAGLE_CONFIG={os.environ['EAGLE_CONFIG']}（开发模式用仓库配置，不读 EAGLE_HOME 里的二进制和配置）")
    print(NEXT_STEPS.format(home=home, xhs_config=paths.xhs_config_path(home)))
    return 0


# ---- 体检 ----

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def _xhs_paths(ch):
    """从 xhs 渠道的 command/cwd 推出二进制和 config.yaml 的实际路径。"""
    cmd = getattr(ch, "command", None) or []
    binary = Path(cmd[0]) if cmd else None
    cfg = None
    if "-config" in cmd:
        i = cmd.index("-config")
        if i + 1 < len(cmd):
            cfg = Path(cmd[i + 1])
            if not cfg.is_absolute():
                cfg = Path(ch.cwd or ".") / cfg
    return binary, cfg


def _yaml_scalar(raw):
    """简单 YAML 标量：去掉行尾注释和引号；空值、~、null 视为 None（未设置）。"""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        q = raw[0]
        out, i = [], 1
        while i < len(raw):
            c = raw[i]
            if q == "'" and c == "'":
                if raw[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            if q == '"' and c == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
                continue
            if q == '"' and c == '"':
                break
            out.append(c)
            i += 1
        return "".join(out)
    raw = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    if raw.startswith("#") or raw in ("", "~", "null", "Null", "NULL"):
        return None
    return raw


def read_browser_config(config):
    """读 xhs config.yaml 里 browser 段的直接子键（不引入 YAML 依赖的简单解析）。
    返回 {键: 字符串或 None}；文件读不到返回 None。
    局限：只认块格式（browser: 换行后缩进的 key: value），不支持流式写法（browser: {...}）、锚点/别名、多行字符串；
    双引号只处理反斜杠转义的单个字符。init / config.example.yaml 生成的配置都是块格式。"""
    try:
        text = Path(config).read_text(encoding="utf-8")
    except OSError:
        return None
    out, in_browser, indent = {}, False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip(" "))
        if cur == 0:
            in_browser = re.match(r"^browser\s*:\s*(#.*)?$", line) is not None
            indent = None
            continue
        if not in_browser:
            continue
        if indent is None:
            indent = cur
        if cur != indent:
            continue  # 更深的缩进（如 cdp_launch 列表项）
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if m:
            out[m.group(1)] = _yaml_scalar(m.group(2))
    return out


def read_cdp_url(config):
    b = read_browser_config(config)
    if b is None:
        return None
    v = b.get("cdp_url")
    return "" if "cdp_url" in b and v is None else v


_YAML_BOOL = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}


def resolve_headless(browser):
    """与 Go 侧 config.ResolveHeadless 一致：显式写了 headless 以它为准；没写时有 cdp_url 的旧配置按 false，否则 true。
    返回 (headless, 说明)。headless 的值无法识别时按未写处理并在说明里提示。"""
    browser = browser or {}
    raw = browser.get("headless")
    note = ""
    if raw is not None:
        v = _YAML_BOOL.get(raw.lower())
        if v is not None:
            return v, f"browser.headless: {raw}"
        note = f"（browser.headless 的值 {raw!r} 无法识别，按未写处理）"
    if browser.get("cdp_url"):
        return False, "未写 browser.headless，已配置 cdp_url 的旧配置按 false" + note
    return True, "未写 browser.headless，默认 true" + note


LINUX_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")


def chrome_candidates(plat=None, env=None):
    """本平台默认的 Chrome 位置（与 eagle-xhs 的 setup.ChromeCandidates 相同顺序）。Linux 没有固定路径，返回空。"""
    plat = sys.platform if plat is None else plat
    env = os.environ if env is None else env
    if plat == "darwin":
        def app(d, name):
            return f"{d}/{name}.app/Contents/MacOS/{name}"
        c = [app("/Applications", "Google Chrome")]
        if env.get("HOME"):
            c.append(app(env["HOME"] + "/Applications", "Google Chrome"))
        return c + [app("/Applications", "Chromium"), app("/Applications", "Microsoft Edge")]
    if plat == "win32":
        pf = env.get("ProgramFiles") or r"C:\Program Files"
        pf86 = env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        c = [pf + r"\Google\Chrome\Application\chrome.exe", pf86 + r"\Google\Chrome\Application\chrome.exe"]
        if env.get("LOCALAPPDATA"):
            c.append(env["LOCALAPPDATA"] + r"\Google\Chrome\Application\chrome.exe")
        return c + [pf86 + r"\Microsoft\Edge\Application\msedge.exe", pf + r"\Microsoft\Edge\Application\msedge.exe"]
    return []


def find_chrome(chrome_path=None, base=None, plat=None, env=None, which=shutil.which):
    """无头模式下 eagle-xhs 会用的浏览器：browser.chrome_path > 平台默认位置 > PATH 中的常见命令名。
    返回 (路径或 None, 说明)。chrome_path 写了但不存在时直接判失败（eagle-xhs 不会再往下找）。"""
    if chrome_path:
        p = Path(chrome_path).expanduser()
        if not p.is_absolute() and base is not None:
            p = Path(base) / p
        if p.is_file():
            return str(p), "browser.chrome_path"
        return None, f"browser.chrome_path={chrome_path} 不存在"
    cands = chrome_candidates(plat, env)
    for c in cands:
        if Path(c).is_file():
            return c, "平台默认位置"
    for name in LINUX_CHROME_NAMES:
        found = which(name)
        if found:
            return found, "PATH"
    checked = cands + [f"PATH 中的 {'/'.join(LINUX_CHROME_NAMES)}"]
    return None, "查找过 " + "、".join(checked)


def probe_cdp(url, timeout=2.0):
    """GET <cdp_url>/json/version（短超时，端口被占但不应答时不挂住）；返回体须含 webSocketDebuggerUrl 才算 Chrome 调试端口。
    返回 (是否可用, 说明)。"""
    import json

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=timeout) as r:  # noqa: S310
            body = r.read(1 << 16).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return False, f"连不上（{type(e).__name__}: {getattr(e, 'reason', e)}）"
    try:
        info = json.loads(body or "{}")
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict) or not info.get("webSocketDebuggerUrl"):
        return False, "端口有响应，但不是 Chrome 调试端口（/json/version 没有 webSocketDebuggerUrl）"
    return True, info.get("Browser") or "可连接"


def run_check(config=None, home=None):
    from .cli import channel_rows
    from .core.registry import Registry

    home = Path(home or paths.eagle_home())
    results = []

    def add(status, label, detail=""):
        results.append((status, label, detail))

    add(OK if home.is_dir() else WARN, "EAGLE_HOME", f"{home}" + ("" if home.is_dir() else "（不存在，运行 eagle setup）"))
    if config:
        cfg_path, source = Path(config), "--config"
    else:
        cfg_path, source = paths.config_path()
    reg = None
    try:
        reg = Registry.load(cfg_path)
        add(OK, "渠道配置", f"{cfg_path}（{source}）")
    except Exception as e:  # noqa: BLE001
        add(FAIL, "渠道配置", f"{cfg_path}（{source}）: {e}")

    xhs = reg.get("xhs") if reg and "xhs" in reg.entries else None
    binary, xhs_cfg = _xhs_paths(xhs) if xhs is not None else (None, None)
    if binary is None:
        binary, xhs_cfg = paths.xhs_binary_path(home), paths.xhs_config_path(home)
    if not binary.is_file():
        add(FAIL, "eagle-xhs", f"{binary} 不存在（运行 eagle setup）")
    else:
        v = binary_version(binary)
        if v == __version__:
            add(OK, "eagle-xhs", f"{binary}（{v}，与包版本一致）")
        elif v and not re.fullmatch(r"\d+\.\d+\.\d+", v):
            add(WARN, "eagle-xhs", f"{binary}（本地构建 {v}，包版本 {__version__}）")
        else:
            add(WARN, "eagle-xhs", f"{binary}（版本 {v or '未知'}，包版本 {__version__}；运行 eagle setup 更新）")
    if xhs_cfg is None or not xhs_cfg.is_file():
        add(FAIL, "小红书配置", f"{xhs_cfg} 不存在（运行 eagle setup）")
    else:
        add(OK, "小红书配置", str(xhs_cfg))

    if reg:
        for row in channel_rows(reg):
            st = OK if row["available"] else WARN
            extra = "" if row["enabled"] else "（已禁用）"
            add(st, f"渠道 {row['name']}", ("可用" if row["available"] else f"不可用: {row['reason']}") + extra)

    try:
        import twscrape  # noqa: F401

        from .channels.x import tws

        db = tws.db_path()
        add(OK if db.exists() else WARN, "X 账号库", f"{db}" + ("" if db.exists() else "（不存在：装好扩展后运行 eagle x login）"))
    except ImportError:
        add(WARN, "X 账号库", "未安装 twscrape（重新安装 eagle-eye）")

    has_cfg = bool(xhs_cfg and xhs_cfg.is_file())
    browser = read_browser_config(xhs_cfg) if has_cfg else None
    headless, why = resolve_headless(browser)
    if headless:
        chrome, detail = find_chrome((browser or {}).get("chrome_path"), base=xhs_cfg.parent if has_cfg else None)
        if chrome:
            add(OK, "Chrome（无头模式）", f"{chrome}（{detail}；{why}）")
        else:
            where = xhs_cfg if has_cfg else "小红书 config.yaml"
            add(WARN, "Chrome（无头模式）", f"找不到 Chrome / Chromium / Edge（{detail}）：请安装 Chrome，"
                f"或在 {where} 设置 browser.chrome_path（不会自动下载；小红书是可选渠道，不影响其他渠道）")
    else:
        cdp = (browser or {}).get("cdp_url")
        if not cdp:
            add(WARN, "Chrome 调试端口", f"{xhs_cfg} 未配置 browser.cdp_url（{why}）")
        else:
            ok, detail = probe_cdp(cdp)
            add(OK if ok else WARN, "Chrome 调试端口",
                f"{cdp}: {detail}（{why}" + ("）" if ok else "；运行 eagle xhs browser 启动）"))

    for status, label, detail in results:
        print(f"[{MARK[status]}] {label}: {detail}")
    failed = any(s == FAIL for s, _, _ in results)
    print("体检未通过：按标 ✗ 项的提示修复（多数运行 eagle setup 即可）" if failed else "体检通过（标 ! 的是提示，不影响基本使用）")
    return 1 if failed else 0


def cmd_setup(args):
    try:
        if args.check:
            return run_check(config=getattr(args, "config", None))
        return run_setup(version=args.version, from_source=args.from_source, force=args.force)
    except (SetupError, RuntimeError, OSError) as e:
        log(f"错误: {e}")
        return 1
