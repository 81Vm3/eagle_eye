# 分发与安装约定

eagle_eye 有两种运行方式：**安装模式**（`uv tool install`，面向使用者）和**开发模式**（源码仓库里的 `bin/eagle`）。
本文是 Python 侧（`eagle_eye/paths.py`、`eagle_eye/setup_cmd.py`）与 Go 侧（`backends/xhs`、release.yml）共同遵守的约定，改动前先和 Commander 确认。

## 安装

```bash
uv tool install git+https://github.com/81Vm3/eagle_eye   # PATH 上出现 eagle（twscrape 一并装好）
eagle setup                                             # 建 EAGLE_HOME、下载并校验 eagle-xhs、生成小红书配置
```

升级：`uv tool upgrade eagle-eye`（或重新 `uv tool install --force git+...`），然后再 `eagle setup` 让 eagle-xhs 与新版本对齐。

## EAGLE_HOME

用户数据/配置根目录。环境变量 `EAGLE_HOME` 优先；否则：

| 平台 | 默认 |
|---|---|
| macOS | `~/Library/Application Support/eagle_eye` |
| Linux（及其他类 Unix） | `${XDG_CONFIG_HOME:-~/.config}/eagle_eye` |
| Windows（预留，本轮未适配） | `%APPDATA%\eagle_eye` |

布局：

```
$EAGLE_HOME/
  eagle.toml            可选：用户自己的渠道注册表，优先于包内默认
  bin/eagle-xhs         小红书渠道二进制（Windows: eagle-xhs.exe），eagle setup 下载
  xhs/config.yaml       小红书配置（eagle-xhs init 生成；其中相对路径按本文件所在目录解析）
  xhs/data/             xhs.db、browser.lock、chrome-profile 等
  x/twscrape.db         X 账号库（旧路径 ~/.config/xsearch/twscrape.db 存在时沿用旧路径）
  x/topics/*.toml       用户的 X 预设主题（同名优先于包内 topics/）
  runs/x/...            eagle x run 的输出（开发模式仍在仓库 runs/）
```

实现上只用标准库，没有引入 platformdirs：规则只有几行，且必须与 Go 侧逐字一致；platformdirs 在各平台的目录名/大小写规则与此不同，
引入后反而要额外覆盖。

## 渠道配置查找

`--config` > `EAGLE_CONFIG` > `$EAGLE_HOME/eagle.toml` > 包内默认 `eagle_eye/defaults/eagle.toml`。

包内默认的小红书登记：

```toml
command = ["{home}/bin/eagle-xhs{exe}", "channel", "-config", "config.yaml"]
cwd = "{home}/xhs"
native_command = ["{home}/bin/eagle-xhs{exe}"]
```

`{home}` = EAGLE_HOME，`{exe}` = Windows 上 `.exe`、其他平台为空，由核心在 `command` / `native_command` / `cwd` 中展开（`eagle_eye/paths.py:expand`）。
展开后仍为相对路径的，相对配置文件所在目录解析（与原规则一致）。

## Release 资产

- GitHub Releases，tag `vX.Y.Z`，与 `pyproject.toml` 的 `version`（= `eagle_eye.__version__`，单测校验二者一致）相同。
- 资产：`eagle-xhs-<goos>-<goarch>[.exe]`，`<goos>-<goarch>` ∈ `darwin-arm64`、`darwin-amd64`、`linux-amd64`、`linux-arm64`、`windows-amd64`；
  另有 `checksums.txt`（sha256sum 格式：`<hex>  <文件名>`）。
- 下载地址：`https://github.com/81Vm3/eagle_eye/releases/download/v<version>/<资产名>`；前缀可用环境变量 `EAGLE_XHS_BASE_URL` 覆盖（测试用，支持 `file://`）。
- 平台映射：`sys.platform` darwin/linux/win32 → darwin/linux/windows；`platform.machine()` x86_64/amd64 → amd64，arm64/aarch64 → arm64。
- `eagle-xhs version` 的 stdout 首行只输出版本号（ldflags `-X main.version=X.Y.Z` 注入，不带 `v`；未注入时为 `dev`）。
- `eagle-xhs init -config <path>`：目标不存在时写入适合当前平台的默认 config.yaml，已存在则不覆盖。
- `eagle-xhs init` 生成的配置显式写 `browser.headless: true`（默认无头：搜索时用专用 profile 自己启动 Chrome，不需要 `cdp_url`）。
- `eagle-xhs login`：有界面窗口扫码，与无头搜索共用专用 profile（`xhs/data/chrome-profile`），由 `browser.lock` 互斥。
- `eagle-xhs browser`：启动带远程调试端口的专用 Chrome（`eagle xhs browser`），仅用于 `headless: false` 或调试；开着时占用专用 profile。

## eagle setup 流程

1. 创建 `$EAGLE_HOME`、`bin/`、`xhs/`、`x/`。
2. 目标版本 = `--version`（默认包版本）。`bin/eagle-xhs version` 已等于目标版本且没有 `--force` → 跳过下载（幂等，不联网）。
3. 否则下载 `checksums.txt` 和当前平台资产（标准库 urllib），sha256 校验通过后 chmod 755 并原子替换；校验失败丢弃下载、保留旧文件，
   错误信息附手动下载地址。`--from-source`：源码仓库 + go 时 `go build -ldflags '-s -w -X main.version=<版本>'` 构建到同一位置。
4. `xhs/config.yaml` 不存在时运行 `eagle-xhs init -config $EAGLE_HOME/xhs/config.yaml`。
5. 打印下一步：X 扩展与登录、小红书 `eagle xhs login`（扫码，弹一次窗口）→ `eagle search -c xhs`、让 Agent 接入（`eagle skill install` / `eagle skill show`，见 README「给 Agent」）。

`eagle setup --check` 只体检、不改文件：EAGLE_HOME、当前使用的渠道配置、渠道实际使用的 eagle-xhs 是否存在及版本是否与包一致、
小红书 config.yaml、各渠道可用性（`eagle channels` 同款判断）、X 账号库，以及小红书浏览器：

- 按 Go 侧 `config.ResolveHeadless` 的规则解析 `browser.headless`（显式值为准；未写时有 `cdp_url` 按 false，否则 true；config.yaml 不存在按 true）。
- headless=true：不探测 CDP，检查本机能否找到 Chrome：`browser.chrome_path`（相对路径按配置目录）> 平台默认位置（macOS `/Applications`、`~/Applications` 下的
  Chrome/Chromium/Edge；Windows Program Files / LOCALAPPDATA）> PATH 中 `google-chrome`、`google-chrome-stable`、`chromium`、`chromium-browser`、`microsoft-edge`。找不到报 !（小红书是可选渠道，不影响 exit code）。
- headless=false：探测 `browser.cdp_url` 的 `/json/version`（连不上只是 !，提示 `eagle xhs browser`）。
- config.yaml 用简单的行解析（不引入 YAML 依赖），只认块格式的 `browser:` 段直接子键；流式写法、锚点、多行字符串不支持（init 生成的配置不会用到）。

有 ✗ 项时 exit 1。

## 包内附带资源

源码只在仓库顶层一份，构建 wheel 时由 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel.force-include]` 打进包内：

| 仓库 | 包内 | 命令 |
|---|---|---|
| `extensions/x-cookie/` | `eagle_eye/_bundled/extensions/x-cookie/` | `eagle x extension [--copy-to DIR] [--open]` |
| `skills/eagle-search/` | `eagle_eye/_bundled/skills/eagle-search/` | `eagle skill install [--agent agents\|claude \| --target DIR] [--copy]`（默认 `~/.agents/skills`）、`eagle skill path`、`eagle skill show` |
| `eagle_eye/defaults/eagle.toml` | 同 | 包内默认渠道配置 |
| `eagle_eye/channels/x/topics/` | 同 | `eagle x topics` |

`eagle_eye/paths.py:bundled()` 先找包内 `_bundled/`，找不到再找源码仓库顶层（开发模式）。检查 wheel 内容：`uv build` 后 `unzip -l dist/*.whl`。

## 开发模式

- 仓库里的 `bin/eagle` 在未设置 `EAGLE_CONFIG` 时把它设为仓库根 `eagle.toml`：小红书仍用仓库 `bin/eagle-xhs`、`backends/xhs/config.yaml`、`backends/xhs/data/`，
  不读 EAGLE_HOME 里的二进制和配置。
- 源码仓库运行时（`eagle_eye/` 的上级目录有 `pyproject.toml` 和 `eagle.toml`）`eagle x run` 输出仍在仓库 `runs/`。
- `make build` 构建 `bin/eagle-xhs`；`make test` 跑 Python + Go 单测；CI（`.github/workflows/ci.yml`）在 ubuntu-latest 和 macos-latest 上跑同样的测试并检查 wheel 内容。
