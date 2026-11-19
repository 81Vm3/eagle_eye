"""渠道注册表：读 eagle.toml，按 type 构造渠道。

    [channels.<name>]
    type = "python"                                  # 进程内渠道
    class = "eagle_eye.channels.x.adapter:XChannel"  # 模块:类，类须继承 core.Channel
    enabled = true                                   # 可选，默认 true
    default = true                                   # 可选，默认 true；false 时只在显式 -c 时搜索

    [channels.<name>]
    type = "external"                                # 外部渠道（JSON-over-stdio，见 docs/channel-protocol.md）
    command = ["bin/eagle-xhs", "channel", ...]
    ...

配置文件位置：EAGLE_CONFIG > $EAGLE_HOME/eagle.toml > 包内默认 eagle_eye/defaults/eagle.toml（见 eagle_eye/paths.py）。
command / native_command / cwd 中的 {home}（EAGLE_HOME）、{exe}（Windows 为 .exe）先展开；
展开后仍是相对路径的，相对配置文件所在目录解析。开发模式下 bin/eagle 把 EAGLE_CONFIG 设为仓库根 eagle.toml。
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from .. import paths
from .channel import BrokenChannel, Channel

ROOT = Path(__file__).resolve().parents[2]
EXPAND_KEYS = ("command", "native_command", "cwd")


def default_config_path():
    return paths.config_path()[0]


def expand_entry(cfg):
    """展开 {home}/{exe} 占位（只处理路径相关的键）。"""
    if not isinstance(cfg, dict):
        return cfg
    return {k: (paths.expand(v) if k in EXPAND_KEYS else v) for k, v in cfg.items()}


def _build_python(name, cfg, root):
    spec = cfg.get("class")
    if not spec or ":" not in spec:
        raise ValueError(f'type="python" 需要 class = "模块:类"，当前 {spec!r}')
    mod_name, cls_name = spec.split(":", 1)
    cls = getattr(importlib.import_module(mod_name), cls_name)
    if not (isinstance(cls, type) and issubclass(cls, Channel)):
        raise TypeError(f"{spec} 不是 eagle_eye.core.Channel 的子类")
    return cls(name, cfg, root)


def _build_external(name, cfg, root):
    from ..channels.external import ExternalChannel

    return ExternalChannel(name, cfg, root)


TYPES = {"python": _build_python, "external": _build_external}


def register_type(type_name, factory):
    """注册新的渠道类型：factory(name, cfg, root) -> Channel。"""
    TYPES[type_name] = factory


class Registry:
    def __init__(self, entries, root=ROOT, path=None):
        self.entries = dict(entries)  # name -> cfg（保持 toml 中的顺序）
        self.root = Path(root)
        self.path = path
        self._cache = {}

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else default_config_path()
        if not path.exists():
            raise FileNotFoundError(f"找不到渠道配置 {path}（可用 EAGLE_CONFIG 或 --config 指定）")
        with open(path, "rb") as f:
            data = tomllib.load(f)
        chans = data.get("channels") or {}
        if not isinstance(chans, dict):
            raise ValueError(f"{path}: [channels] 格式不对")
        chans = {name: expand_entry(cfg) for name, cfg in chans.items()}
        return cls(chans, root=path.resolve().parent, path=path)

    def names(self):
        return list(self.entries)

    def is_enabled(self, name):
        return bool(self.entries.get(name, {}).get("enabled", True))

    def enabled_names(self):
        return [n for n in self.entries if self.is_enabled(n)]

    def is_default(self, name):
        """不带 -c 的 eagle search 是否搜这个渠道：enabled 且 default（默认 true）。"""
        return self.is_enabled(name) and bool(self.entries.get(name, {}).get("default", True))

    def default_names(self):
        return [n for n in self.entries if self.is_default(n)]

    def get(self, name) -> Channel:
        if name not in self.entries:
            raise KeyError(f"未知渠道 {name}（已登记: {', '.join(self.entries) or '无'}）")
        if name not in self._cache:
            cfg = self.entries[name]
            factory = TYPES.get(cfg.get("type"))
            try:
                if factory is None:
                    raise ValueError(f"未知渠道类型 type={cfg.get('type')!r}（可选 {', '.join(TYPES)}）")
                ch = factory(name, cfg, self.root)
            except Exception as e:  # noqa: BLE001 — 单个渠道配置坏了不影响其他渠道
                ch = BrokenChannel(name, cfg, self.root, reason=f"渠道构造失败: {type(e).__name__}: {e}")
            self._cache[name] = ch
        return self._cache[name]
