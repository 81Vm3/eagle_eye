# eagle_eye 顶层任务
#   make venv    用 uv 建 .venv 并装 twscrape（X 渠道 twscrape 后端）
#   make build   构建小红书外部渠道 → bin/eagle-xhs（委托 backends/xhs/Makefile）
#   make test    Python 单测 + Go 单测（均不访问网络）

PY      ?= python3
# Go 不一定在 PATH（本机在 /usr/local/go/bin）：PATH 优先，其次 /usr/local/go/bin/go
GO      ?= $(shell command -v go 2>/dev/null || { [ -x /usr/local/go/bin/go ] && echo /usr/local/go/bin/go; } || echo go)
UV      ?= $(shell command -v uv 2>/dev/null || { [ -x $(HOME)/.local/bin/uv ] && echo $(HOME)/.local/bin/uv; } || echo uv)

.DEFAULT_GOAL := help
.PHONY: help venv build test test-py test-go clean

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv 并安装 twscrape
	$(UV) venv .venv
	$(UV) pip install --python .venv/bin/python twscrape

build: ## 构建小红书渠道二进制 bin/eagle-xhs
	$(MAKE) -C backends/xhs build GO=$(GO)

test: test-py test-go ## 全部测试

test-py: ## Python 单测
	$(PY) -m unittest discover -s tests -t .

test-go: ## Go 单测（backends/xhs）
	$(MAKE) -C backends/xhs test GO=$(GO)

clean: ## 删除构建产物和缓存（不删 runs/、.venv）
	rm -f bin/eagle-xhs
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
