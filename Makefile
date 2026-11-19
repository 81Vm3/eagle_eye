# eagle_eye 顶层任务
#   make venv    用 uv 建 .venv 并装 twscrape（X 渠道 twscrape 后端）
#   make build   构建小红书外部渠道 → bin/eagle-xhs（委托 backends/xhs/Makefile）
#   make test    Python 单测 + Go 单测（均不访问网络）
#   make test-ci 本地复现 CI 的 Python 单测：干净副本 + 新 venv + 空 HOME + env -i
#   make sync-extension EXT_DEST=<目录>  把 X cookie 扩展复制到浏览器实际加载的目录（= bin/eagle x extension --copy-to）

PY      ?= python3
# Go：PATH 优先；不在 PATH 时用 Go 官方安装包的默认位置，也可 make GO=/path/to/go
GO      ?= $(shell command -v go 2>/dev/null || { [ -x /usr/local/go/bin/go ] && echo /usr/local/go/bin/go; } || echo go)
# 调 backends/xhs 时把 GO 所在目录放进 PATH（其 go test 里有调用 go build 的测试，PATH 找不到 go 会 skip）
GO_PATH_PREFIX = $(if $(findstring /,$(GO)),PATH="$(abspath $(dir $(GO))):$$PATH" ,)
UV      ?= $(shell command -v uv 2>/dev/null || { [ -x $(HOME)/.local/bin/uv ] && echo $(HOME)/.local/bin/uv; } || echo uv)
# 浏览器加载的扩展副本目录（必填，例：WSL 里给 Windows 浏览器用 /mnt/c/Users/<用户名>/eagle-x-cookie）
EXT_DEST ?=
# test-ci 用的 Python 版本（与 .github/workflows/ci.yml 一致）
CI_PYTHON ?= 3.11

.DEFAULT_GOAL := help
.PHONY: help venv build test test-py test-go test-ci sync-extension clean

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv 并安装 twscrape（开发模式；安装模式见 README「macOS 快速开始」）
	$(UV) venv .venv
	$(UV) pip install --python .venv/bin/python twscrape

build: ## 构建小红书渠道二进制 bin/eagle-xhs
	$(GO_PATH_PREFIX)$(MAKE) -C backends/xhs build GO=$(GO)

test: test-py test-go ## 全部测试

test-py: ## Python 单测
	$(PY) -m unittest discover -s tests -t .

test-go: ## Go 单测（backends/xhs）
	$(GO_PATH_PREFIX)$(MAKE) -C backends/xhs test GO=$(GO)

test-ci: ## 本地复现 CI：只拷 git 跟踪/未忽略的文件（含未提交改动）到临时目录，新建 Python $(CI_PYTHON) venv，空 HOME + env -i 跑 test-py
	@set -e; tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	git ls-files -z -co --exclude-standard | python3 -c 'import os, shutil, sys; d = sys.argv[1]; \
	[(os.makedirs(os.path.join(d, os.path.dirname(f)), exist_ok=True), shutil.copy2(f, os.path.join(d, f))) \
	 for f in sys.stdin.read().split("\0") if f and os.path.isfile(f)]' "$$tmp/src"; \
	$(UV) venv -q -p $(CI_PYTHON) "$$tmp/venv"; \
	$(UV) pip install -q --python "$$tmp/venv/bin/python" twscrape; \
	mkdir "$$tmp/home"; \
	echo "test-ci: 源码副本 $$tmp/src，HOME=$$tmp/home" >&2; \
	cd "$$tmp/src" && env -i HOME="$$tmp/home" PATH="$$tmp/venv/bin:/usr/bin:/bin" LANG=C.UTF-8 make test-py PY=python

sync-extension: ## 复制 X cookie 扩展到 EXT_DEST（必填，只覆盖同名文件），之后在浏览器扩展页点「重新加载」
	@test -n "$(EXT_DEST)" || { echo "用法: make sync-extension EXT_DEST=<浏览器加载的扩展目录>" >&2; exit 2; }
	bin/eagle x extension --copy-to "$(EXT_DEST)"

clean: ## 删除构建产物和缓存（不删 runs/、.venv）
	rm -f bin/eagle-xhs
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
