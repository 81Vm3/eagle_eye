# eagle_eye 小红书搜索渠道后端。二进制构建到仓库根目录 bin/eagle-xhs。
# 用法示例：
#   make                       # = make build → ../../bin/eagle-xhs
#   make describe              # 输出渠道 Descriptor（eagle_eye 协议）
#   make search Q='Claude Code 教程'   # 一次渠道搜索（真实访问小红书，有封号风险，慎用）
#   make login                 # 打开浏览器手动扫码登录
#   make install               # 安装到 ~/.local/bin/eagle-xhs
#   make dist                  # 交叉编译 5 个 Release 资产 + checksums.txt 到 dist/（与 release.yml 相同）

BINARY  := eagle-xhs
BIN_DIR := ../../bin
PREFIX  ?= $(HOME)/.local
CONFIG  ?= config.yaml

V       ?=
Q       ?=
DETAIL  ?= 3

GO      ?= go
GOFMT   ?= $(shell $(GO) env GOROOT)/bin/gofmt
# 版本号不带 v 前缀（tag v1.2.3 → 1.2.3）；无 git 信息时为 dev。eagle-xhs version 输出它
VERSION ?= $(patsubst v%,%,$(shell git describe --tags --always --dirty 2>/dev/null || echo dev))
LDFLAGS := -s -w -X main.version=$(VERSION)

# Release 资产：eagle-xhs-<goos>-<goarch>[.exe]（命名与 eagle setup 的下载约定一致，改动需同步 Python 侧）
DIST_DIR     := dist
DIST_TARGETS := darwin-arm64 darwin-amd64 linux-amd64 linux-arm64 windows-amd64

.DEFAULT_GOAL := build
.PHONY: build dist install uninstall login describe search test fmt vet check tidy clean config help

build: ## 编译到 ../../bin/eagle-xhs
	$(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN_DIR)/$(BINARY) .

dist: ## 交叉编译全部 Release 资产 + checksums.txt 到 dist/（VERSION=1.2.3 可覆盖版本）
	rm -rf $(DIST_DIR) && mkdir -p $(DIST_DIR)
	@set -e; for t in $(DIST_TARGETS); do \
		os=$${t%-*}; arch=$${t#*-}; ext=; [ "$$os" = windows ] && ext=.exe; \
		out=$(DIST_DIR)/$(BINARY)-$$os-$$arch$$ext; echo "build $$out"; \
		CGO_ENABLED=0 GOOS=$$os GOARCH=$$arch $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $$out .; \
	done
	cd $(DIST_DIR) && if command -v sha256sum >/dev/null; then sha256sum $(BINARY)-* > checksums.txt; \
		else shasum -a 256 $(BINARY)-* > checksums.txt; fi
	@cat $(DIST_DIR)/checksums.txt

install: build ## 安装到 $(PREFIX)/bin
	install -d $(PREFIX)/bin
	install -m 755 $(BIN_DIR)/$(BINARY) $(PREFIX)/bin/$(BINARY)
	@echo "已安装 $(PREFIX)/bin/$(BINARY)（运行时用 -config 指定配置文件）"

uninstall: ## 卸载
	rm -f $(PREFIX)/bin/$(BINARY)

config: ## 没有 config.yaml 时从示例生成
	@test -f $(CONFIG) || (cp config.example.yaml $(CONFIG) && echo "已生成 $(CONFIG)，请按需修改")

describe: build ## 输出渠道 Descriptor
	$(BIN_DIR)/$(BINARY) channel -config $(CONFIG) describe

search: build config ## 渠道搜索一次（真实访问小红书），Q=关键词 DETAIL=3 V=1
	@test -n "$(Q)" || (echo "用法: make search Q=关键词" && exit 2)
	printf '{"protocol":1,"text":"%s","options":{"detail":%s}}' "$(Q)" "$(DETAIL)" | $(BIN_DIR)/$(BINARY) channel -config $(CONFIG) $(if $(V),-v) search

login: build config ## 打开浏览器手动扫码登录
	$(BIN_DIR)/$(BINARY) login -config $(CONFIG) $(if $(V),-v)

test: ## 单元测试
	$(GO) test ./...

fmt: ## gofmt 格式化
	$(GOFMT) -w .

vet: ## go vet
	$(GO) vet ./...

check: fmt vet test ## 格式化 + vet + 测试

tidy: ## 整理依赖
	$(GO) mod tidy

clean: ## 删除编译产物（不删除 data/）
	rm -f $(BIN_DIR)/$(BINARY)
	rm -rf $(DIST_DIR)

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
