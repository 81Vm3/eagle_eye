"""用户数据目录（EAGLE_HOME）、配置查找、包内附带资源的位置。只用标准库（不引入 platformdirs，
避免核心多一个依赖；需要的规则只有几行，且必须和 Go 侧 eagle-xhs 完全一致，见 docs/distribution.md）。

EAGLE_HOME：环境变量 EAGLE_HOME 优先；否则
    macOS   ~/Library/Application Support/eagle_eye
    Linux   ${XDG_CONFIG_HOME:-~/.config}/eagle_eye
    Windows %APPDATA%\\eagle_eye

配置查找：EAGLE_CONFIG > $EAGLE_HOME/eagle.toml > 包内默认 eagle_eye/defaults/eagle.toml。
配置里 command / native_command / cwd 中的 {home}、{exe} 由 expand() 展开。
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "defaults" / "eagle.toml"
# 打包时 extensions/、skills/ 被构建配置放到这里（见 pyproject 的 force-include）；源码仓库里不存在
BUNDLED_DIR = PACKAGE_DIR / "_bundled"

XHS_BINARY = "eagle-xhs"


def eagle_home(environ=None, plat=None, home=None) -> Path:
    environ = os.environ if environ is None else environ
    plat = sys.platform if plat is None else plat
    home = Path(home) if home else Path.home()
    if environ.get("EAGLE_HOME"):
        return Path(environ["EAGLE_HOME"]).expanduser()
    if plat == "darwin":
        return home / "Library" / "Application Support" / "eagle_eye"
    if plat == "win32":
        appdata = environ.get("APPDATA")
        return (Path(appdata) if appdata else home / "AppData" / "Roaming") / "eagle_eye"
    xdg = environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else home / ".config") / "eagle_eye"


def exe_suffix(plat=None) -> str:
    return ".exe" if (sys.platform if plat is None else plat) == "win32" else ""


def xhs_binary_path(home=None) -> Path:
    return Path(home or eagle_home()) / "bin" / f"{XHS_BINARY}{exe_suffix()}"


def xhs_config_path(home=None) -> Path:
    return Path(home or eagle_home()) / "xhs" / "config.yaml"


def source_root() -> Path | None:
    """从源码仓库运行（bin/eagle、python -m、可编辑安装）时返回仓库根，安装成 wheel 时返回 None。"""
    root = PACKAGE_DIR.parent
    if (root / "pyproject.toml").is_file() and (root / "eagle.toml").is_file() and (root / "eagle_eye").is_dir():
        return root
    return None


def config_path(environ=None) -> tuple[Path, str]:
    """返回 (配置文件路径, 来源说明)。"""
    environ = os.environ if environ is None else environ
    if environ.get("EAGLE_CONFIG"):
        return Path(environ["EAGLE_CONFIG"]).expanduser(), "EAGLE_CONFIG"
    user = eagle_home(environ) / "eagle.toml"
    if user.is_file():
        return user, "EAGLE_HOME"
    return DEFAULT_CONFIG, "包内默认"


def expand(value, home=None, plat=None):
    """展开字符串里的 {home}、{exe}；列表逐项展开；其他类型原样返回。用 replace 而非 format，其他花括号不受影响。"""
    if isinstance(value, list):
        return [expand(v, home, plat) for v in value]
    if not isinstance(value, str):
        return value
    if "{home}" in value:
        value = value.replace("{home}", str(home or eagle_home()))
    if "{exe}" in value:
        value = value.replace("{exe}", exe_suffix(plat))
    return value


def bundled(*parts) -> Path:
    """包内附带的资源（extensions/x-cookie、skills/eagle-search）：安装后在 _bundled/ 下，源码仓库里在顶层。"""
    p = BUNDLED_DIR.joinpath(*parts)
    if p.exists():
        return p
    root = source_root()
    if root and root.joinpath(*parts).exists():
        return root.joinpath(*parts)
    raise FileNotFoundError(f"找不到包内资源 {'/'.join(parts)}（安装包不完整？请重新安装 eagle-eye）")


def runs_dir() -> Path:
    """eagle x run 的输出根目录：源码仓库里是 runs/，安装模式是 $EAGLE_HOME/runs。"""
    root = source_root()
    return (root / "runs") if root else eagle_home() / "runs"


def goos_goarch(plat=None, machine=None) -> tuple[str, str]:
    """当前平台对应的 Release 资产名片段；不支持时抛 RuntimeError。"""
    plat = sys.platform if plat is None else plat
    machine = (platform.machine() if machine is None else machine).lower()
    goos = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(plat)
    goarch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not goos or not goarch:
        raise RuntimeError(f"没有适用于当前平台的 eagle-xhs 预编译版本（{plat}/{machine}）")
    return goos, goarch
