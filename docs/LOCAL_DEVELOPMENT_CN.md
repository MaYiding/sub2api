# Sub2API 本机开发环境

本机开发采用以下结构：

- PostgreSQL 18：Homebrew 登录服务，监听 `127.0.0.1:5432`
- Redis 8：Homebrew 登录服务，监听 `127.0.0.1:6379`，Sub2API 使用 DB 15
- Go 后端：本机编译运行，监听 `127.0.0.1:8080`
- Vite 前端：本机运行，监听 `127.0.0.1:3000`

所有日常操作统一通过仓库根目录下的脚本执行：

```bash
./tools/sub2api-dev.sh start
./tools/sub2api-dev.sh status
./tools/sub2api-dev.sh stop
./tools/sub2api-dev.sh restart
```

## 日志

```bash
./tools/sub2api-dev.sh logs backend
./tools/sub2api-dev.sh logs frontend
```

前后端由 macOS 自带的 `screen` 托管，因此关闭启动命令所在终端后仍会继续运行。日志和后端二进制保存在 Git 已忽略的 `deploy/data/run/`。

## 备份

```bash
./tools/sub2api-dev.sh backup
```

备份默认保存在：

```text
~/.local/share/sub2api/backups/<时间戳>/
```

每份备份包含 PostgreSQL 自定义格式备份、`deploy/.env` 和应用运行配置。备份包含敏感凭据，目录和文件权限会自动限制为当前用户可读。

## 完整重置

完整重置会删除以下本机开发数据：

- PostgreSQL 中名为 `sub2api` 的数据库
- Redis DB 15
- `deploy/data/` 中的配置、安装锁、日志和运行文件

它不会删除其他 PostgreSQL 数据库，也不会执行 Redis `FLUSHALL`。运行前会自动备份。

```bash
./tools/sub2api-dev.sh reset --yes
```

重置后脚本会重新初始化数据库、创建管理员，并创建新的 `Local Development` API 密钥写回 `deploy/.env`。

## API 密钥

额外创建 API 密钥：

```bash
./tools/sub2api-dev.sh create-key "密钥名称"
```

新密钥会显示在终端，并写入 Git 已忽略的 `deploy/.env` 中的 `SUB2API_API_KEY`。

## 停止基础设施

普通 `stop` 只停止前后端，PostgreSQL 和 Redis 继续作为 Homebrew 服务运行：

```bash
./tools/sub2api-dev.sh stop
```

如需同时停止数据库和 Redis：

```bash
./tools/sub2api-dev.sh stop --all
```

下次执行 `start` 时会自动重新启动它们。

## Codex OAuth 的 Claude API 兼容入口

当前本机实例可以把 Anthropic Messages 请求转换后转发到 OpenAI Codex OAuth 账户。专用 API Key 保存在 `deploy/.env` 的 `SUB2API_CODEX_API_KEY`，不要把该值提交到 Git。

同一台 Mac 上的 Claude Code 或供应商管理工具按下面填写：

| 字段 | 推荐值 |
| --- | --- |
| API Key | `deploy/.env` 中的 `SUB2API_CODEX_API_KEY` |
| 请求地址 | `http://127.0.0.1:8080` |
| 完整 URL | 关闭 |
| API 格式 | `Anthropic Messages（原生）` |
| 认证字段 | `ANTHROPIC_AUTH_TOKEN` |

推荐模型映射：

| 模型角色 | 显示名称 | 实际请求模型 | 1M |
| --- | --- | --- | --- |
| Sonnet | `GPT-5.4 (Codex OAuth)` | `gpt-5.4` | 不勾选 |
| Opus | `GPT-5.4 (Codex OAuth)` | `gpt-5.4` | 不勾选 |
| Fable | `GPT-5.4 Mini (Codex OAuth)` | `gpt-5.4-mini` | 不勾选 |
| Haiku | `GPT-5.4 Mini (Codex OAuth)` | `gpt-5.4-mini` | 不勾选 |

如果调用方运行在 Docker 容器内，请把请求地址改成 `http://host.docker.internal:8080`。当前后端只监听本机回环地址，不应直接作为公网服务暴露。

快速验证：

```bash
set -a
source deploy/.env
set +a

curl http://127.0.0.1:8080/v1/messages \
  -H "Authorization: Bearer $SUB2API_CODEX_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  --data '{"model":"gpt-5.4","max_tokens":32,"messages":[{"role":"user","content":"Reply with OK"}]}'
```
