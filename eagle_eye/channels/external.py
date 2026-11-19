"""通用外部渠道：按 docs/channel-protocol.md 的 JSON-over-stdio 协议调用任意语言的渠道程序。

eagle.toml：

    [channels.xhs]
    type = "external"
    command = ["bin/eagle-xhs", "channel", "-config", "config.yaml"]   # config.yaml 不含 "/"，相对子进程 cwd
    cwd = "backends/xhs"            # 可选，子进程工作目录
    timeout_seconds = 900           # 可选，单次 search 超时（默认 300）
    describe_timeout_seconds = 30   # 可选，describe 超时
    native_command = ["bin/eagle-xhs"]  # 可选，eagle <name> ... 原样透传给它
    title / description / cost_hint     # 可选，二进制不可用时 eagle channels 展示的兜底信息

路径规则：command / native_command 里不以 "-" 开头、不含 "://"、含 "/" 的相对路径参数，
以及 cwd，都按仓库根目录解析成绝对路径（子进程 cwd 改变后仍指向同一文件）。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from ..core.channel import Channel
from ..core.models import INTERNAL, UNAVAILABLE, ChannelError, Descriptor, SearchResult

STDERR_TAIL_LINES = 20
DEFAULT_TIMEOUT = 300
DEFAULT_DESCRIBE_TIMEOUT = 30


def _stderr(msg):
    print(msg, file=sys.stderr, flush=True)


def resolve_arg(arg, root):
    s = str(arg)
    if s.startswith("-") or "://" in s or "/" not in s or os.path.isabs(s):
        return s
    return str(Path(root) / s)


class ExternalChannel(Channel):
    def __init__(self, name, config=None, root=None, log=_stderr):
        super().__init__(name, config, root or Path.cwd())
        cmd = self.config.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise ValueError(f"渠道 {name}: command 必须是非空字符串数组")
        self.command = [resolve_arg(c, self.root) for c in cmd]
        native = self.config.get("native_command")
        self.native_command = [resolve_arg(c, self.root) for c in native] if native else None
        cwd = self.config.get("cwd")
        self.cwd = str(Path(self.root) / cwd) if cwd else None
        self.timeout = float(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        self.describe_timeout = float(self.config.get("describe_timeout_seconds") or DEFAULT_DESCRIBE_TIMEOUT)
        self.log = log
        self._descriptor = None

    # ---- 子进程 ----

    def _executable_error(self, argv0):
        if os.sep in argv0:
            if not os.path.exists(argv0):
                return f"找不到可执行文件 {argv0}（外部渠道未构建？顶层运行 make build）"
            if not os.access(argv0, os.X_OK):
                return f"{argv0} 没有执行权限"
        elif shutil.which(argv0) is None:
            return f"PATH 中找不到 {argv0}"
        if self.cwd and not os.path.isdir(self.cwd):
            return f"工作目录不存在: {self.cwd}"
        return None

    def _call(self, verb, payload, timeout, stream_stderr):
        """执行 command + [verb]，返回解析后的 stdout JSON 对象；协议层面的失败一律抛 ChannelError。"""
        argv = self.command + [verb]
        err = self._executable_error(argv[0])
        if err:
            raise ChannelError(UNAVAILABLE, err, channel=self.name)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                start_new_session=True,  # 超时时连同子进程组一起杀掉
            )
        except OSError as e:
            raise ChannelError(UNAVAILABLE, f"无法启动 {argv[0]}: {e}", channel=self.name) from e

        tail = deque(maxlen=STDERR_TAIL_LINES)
        out_chunks = []

        def pump_stderr():
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                tail.append(line)
                if stream_stderr:
                    self.log(f"[{self.name}] {line}")

        def pump_stdout():
            out_chunks.append(proc.stdout.read())

        threads = [threading.Thread(target=pump_stderr, daemon=True), threading.Thread(target=pump_stdout, daemon=True)]
        for t in threads:
            t.start()
        try:
            if payload is not None:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # 子进程没读 stdin 就退出了，交给下面按退出码处理
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            for t in threads:
                t.join(2)
            self._close_pipes(proc)
            raise ChannelError(
                INTERNAL, f"{verb} 超时（{timeout:g} 秒），已终止子进程{self._tail_text(tail)}", channel=self.name
            ) from None
        for t in threads:
            t.join(5)
        self._close_pipes(proc)

        stdout = b"".join(c for c in out_chunks if c).decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            obj = None
        if not isinstance(obj, dict):
            what = "无输出" if not stdout else f"stdout 不是合法 JSON 对象: {stdout[:200]!r}"
            raise ChannelError(INTERNAL, f"{verb} exit {proc.returncode}，{what}{self._tail_text(tail)}", channel=self.name)
        if obj.get("error"):
            raise ChannelError.from_dict(obj["error"], channel=self.name)
        if proc.returncode != 0:
            raise ChannelError(
                INTERNAL, f"{verb} exit {proc.returncode} 且未给出 error 对象{self._tail_text(tail)}", channel=self.name
            )
        return obj

    @staticmethod
    def _close_pipes(proc):
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass

    @staticmethod
    def _tail_text(tail):
        return ("\nstderr 末尾:\n" + "\n".join(tail)) if tail else ""

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(5)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    # ---- Channel 接口 ----

    def fallback_descriptor(self):
        """二进制不可用时 eagle channels 用来展示的信息（来自 eagle.toml）。"""
        return Descriptor(
            name=self.name,
            title=str(self.config.get("title") or ""),
            description=str(self.config.get("description") or ""),
            cost_hint=str(self.config.get("cost_hint") or ""),
        )

    def describe(self):
        if self._descriptor is None:
            obj = self._call("describe", None, self.describe_timeout, stream_stderr=False)
            try:
                self._descriptor = Descriptor.from_dict(obj)
            except ValueError as e:
                raise ChannelError(INTERNAL, f"describe 输出不合法: {e}", channel=self.name) from e
        return self._descriptor

    def search(self, query):
        query.validate()
        obj = self._call("search", query.to_dict(), self.timeout, stream_stderr=True)
        return SearchResult.from_dict(obj, channel=self.name)

    def native(self, argv):
        if not self.native_command:
            return super().native(argv)
        err = self._executable_error(self.native_command[0])
        if err:
            raise ChannelError(UNAVAILABLE, err, channel=self.name)
        try:
            return subprocess.call(self.native_command + list(argv), cwd=self.cwd)
        except KeyboardInterrupt:
            return 130
