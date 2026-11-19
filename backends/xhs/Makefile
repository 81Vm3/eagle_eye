# eagle_eye 小红书搜索渠道后端。二进制构建到仓库根目录 bin/eagle-xhs。
# 用法示例：
#   make                       # = make build → ../../bin/eagle-xhs
#   make describe              # 输出渠道 Descriptor（eagle_eye 协议）
#   make search Q='Claude Code 教程'   # 一次渠道搜索（真实访问小红书，有封号风险，慎用）
#   make login                 # 打开浏览器手动扫码登录
#   make install               # 安装到 ~/.local/bin/eagle-xhs

BINARY  := eagle-xhs
BIN_DIR := ../../bin
PREFIX  ?= $(HOME)/.local
CONFIG  ?= config.yaml

V       ?=
Q       ?=
DETAIL  ?= 3

GO      ?= go
LDFLAGS := -s -w

.DEFAULT_GOAL := build
.PHONY: build install uninstall login describe search test fmt vet check tidy clean config help

build: ## 编译到 ../../bin/eagle-xhs
	$(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN_DIR)/$(BINARY) .

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
	gofmt -w .

vet: ## go vet
	$(GO) vet ./...

check: fmt vet test ## 格式化 + vet + 测试

tidy: ## 整理依赖
	$(GO) mod tidy

clean: ## 删除编译产物（不删除 data/）
	rm -f $(BIN_DIR)/$(BINARY)

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
