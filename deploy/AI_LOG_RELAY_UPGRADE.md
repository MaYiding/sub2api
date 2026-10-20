# 中转站 AI 日志采集升级指南：v0.2.15-ailog.20261020

面向运行本分支、已开启 AI 日志采集的外部中转站运维人员。
前提：站点已按 [BLUE_GREEN.md](BLUE_GREEN.md) 做过蓝绿发布，shipper 已按 [AI_LOGGING.md](AI_LOGGING.md) 安装并在运行。

## 1. 适用范围与变更摘要

本次只换应用镜像。tag `v0.2.15-ailog.20261020`（附注 tag，位于 `dev` 分支）。

1. `capture.end` 新增 `wal_dropped_events`：WAL 写入器已接收、但没能落盘的事件数（例如 spool 已满），只在非零时出现。以前这类丢失在单个请求上看不到。
2. 记录 Claude Code 的 `X-Claude-Code-Session-Id`，并优先用作 trace ID；`X-Claude-Code-Agent-Id`、`X-Claude-Code-Parent-Agent-Id` 一并保留。
3. **默认行为变化：上游尝试的请求/响应正文默认不再复制。** 新增可选变量 `AI_LOG_UPSTREAM_BODIES=true|false`，默认 `false`。关闭时每次上游尝试仍记录状态码、字节数和 sha256。上游正文约占采集总量的一半，默认关闭后 WAN、Kafka 和本地 spool 负载约减半。这是缓解 spool 写满的主要手段。
4. `deploy/blue-green-deploy.sh` 会像其他 `AI_LOG_*` 设置一样，把 `AI_LOG_UPSTREAM_BODIES` 带到新容器。

shipper（`tools/ai-log-pipeline/shipper.py`、`deploy/install-ai-log-shipper.sh`）本版本未改动，无需升级。

## 2. 拉取指定 tag 并构建镜像

在单独的构建机上构建，不要在公网生产中转站上构建。构建机需要 git 和支持 BuildKit 的 Docker（Dockerfile 使用了 `RUN --mount`）。

```bash
git clone https://github.com/MaYiding/sub2api.git
cd sub2api
# 已有仓库时，用这一行代替 clone：git fetch --tags origin
git checkout v0.2.15-ailog.20261020
git describe --tags --exact-match   # 应输出 v0.2.15-ailog.20261020
git rev-parse HEAD                  # 应输出 1db027c9548d4339a1a830e5986c6bf559a374b0
```

`.dockerignore` 排除了 `.git`，构建时读不到 tag，版本会退回 `backend/cmd/server/VERSION` 里的 `0.2.15`。所以版本号和提交号要用构建参数传入：

```bash
docker build --platform linux/amd64 \
  -f Dockerfile \
  --build-arg VERSION=0.2.15-ailog.20261020 \
  --build-arg COMMIT="$(git rev-parse --short HEAD)" \
  -t sub2api:0.2.15-ailog.20261020 \
  .
```

- `DATE` 不传时取构建时间。Dockerfile 默认 `GOPROXY=https://goproxy.cn,direct`，需要别的代理时加 `--build-arg GOPROXY=...`（写法见 `deploy/build_image.sh`）。
- 蓝绿脚本只接受 `linux/amd64` 镜像。在 ARM 机器上构建也要保留 `--platform linux/amd64`。

把镜像送到中转站：推到你们自己的私有镜像仓库后在中转站上拉取（蓝绿脚本本身不会拉镜像），或者不经仓库直接传（远端账号需能执行 docker）：

```bash
docker save sub2api:0.2.15-ailog.20261020 | gzip | ssh <中转站主机> 'gunzip | docker load'
```

在中转站上自检：

```bash
docker image inspect sub2api:0.2.15-ailog.20261020 --format '{{.Os}}/{{.Architecture}}'   # linux/amd64
docker run --rm --entrypoint /app/sub2api sub2api:0.2.15-ailog.20261020 --version 2>&1      # 含 0.2.15-ailog.20261020
```

## 3. 蓝绿发布

本次只换镜像：

- 不需要新密钥，也不需要新配置。现有 g2 shipper 配置 `/etc/sub2api-ai-log/shipper.json`（权限 0600）不动。它含 Kafka 凭据，不要查看、复制或外发。
- `AI_LOG_ENABLED`、`AI_LOG_SOURCE_ID`、`AI_LOG_SPOOL_MAX_BYTES` 和 `/app/ai-log-spool` 挂载由脚本从当前运行的容器继承。
- 不要重跑 `install-ai-log-shipper.sh`，不要改 shipper 配置，不要清空或移动 spool 目录。积压数据要保留，shipper 会继续发送。
- 切换期间新旧容器会短暂同时写同一个 spool，这是设计行为。

### 3.1 发布前确认

在中转站上，用平时发布所用的账号执行（设置过 `SUB2API_DEPLOY_DIR` 的按实际目录改）：

```bash
STATE=/opt/sub2api/deploy/.blue-green-active
cat "$STATE"
ACTIVE=$(sed -n 's/^active_container=//p' "$STATE")
docker inspect "$ACTIVE" --format '{{.Config.Image}}'     # 记下，回滚用
docker inspect "$ACTIVE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AI_LOG_'
SPOOL=$(docker inspect "$ACTIVE" --format '{{range .Mounts}}{{if eq .Destination "/app/ai-log-spool"}}{{.Source}}{{end}}{{end}}')
echo "$SPOOL"                                              # spool 的宿主机目录
docker ps -a --filter name=sub2api
```

- 应看到 `AI_LOG_ENABLED=true`、`AI_LOG_SOURCE_ID=...`，`SPOOL` 不为空。不符合就先停下，联系系统管理员。
- 脚本要求目标槽位容器不存在：当前是 `sub2api-green` 时目标为 `sub2api-blue`，反之亦然。若目标容器已存在且为 Exited（以前发布留下的旧版本），脚本会报 `target container already exists` 并退出。确认它不是当前活动容器后删除（数据目录会保留）：`docker rm sub2api-blue`（按实际名称）。

### 3.2 执行

先用本 tag 的 `deploy/blue-green-deploy.sh` 替换中转站上原来使用的脚本副本。旧脚本不会传递 `AI_LOG_UPSTREAM_BODIES`。

```bash
scp deploy/blue-green-deploy.sh <中转站主机>:<原脚本所在目录>/blue-green-deploy.sh
```

在中转站上运行。`SUB2API_CADDY_SITE_HOST` 每次都必须设置，Caddy 相关变量与上次发布保持一致：

```bash
export SUB2API_CADDY_SITE_HOST='<与上次发布相同>'
# 上次改过默认值时才需要：
# export SUB2API_CADDYFILE=/opt/sub2api-https/Caddyfile
# export SUB2API_CADDY_CONTAINER=sub2api-https
./blue-green-deploy.sh sub2api:0.2.15-ailog.20261020
# 同时调大 spool 上限时（见第 4 节）：
# AI_LOG_SPOOL_MAX_BYTES=42949672960 ./blue-green-deploy.sh sub2api:0.2.15-ailog.20261020
```

脚本先检查：镜像已在本机且为 `linux/amd64`；Caddyfile 含 `lb_policy first`、`health_uri /health`、`stream_close_delay`；采集开启时 spool 是已存在的绝对路径、不是软链、不在应用数据目录内；主机锁保证同时只有一个发布。

然后复制 `/app/data` 给新容器，等待 Docker 健康检查和 `/health` 通过（默认 180 秒），切换 Caddy，再观察 30 秒。任一步失败会恢复 Caddy、删除失败的新容器，旧容器继续服务。成功后写状态文件，旧容器排空 60 秒后停止，不删除。

### 3.3 回滚

先按 3.1 删除已停止的目标槽位容器，再用升级前记下的镜像跑一次脚本：

```bash
./blue-green-deploy.sh <升级前的镜像>
```

采集设置和 spool 挂载照常继承。旧镜像不认识 `AI_LOG_UPSTREAM_BODIES`，会恢复为总是复制上游正文，spool 负载回到升级前水平。

只想停采集：

```bash
AI_LOG_ENABLED=false ./blue-green-deploy.sh <镜像>
```

新容器不再挂载 spool。spool 和 shipper 保留，等积压发完。之后重新启用时，要同时给出 `AI_LOG_ENABLED=true` 和 `SUB2API_AI_LOG_SPOOL_HOST_DIR=<spool 宿主机目录>`，因为停采集的容器没有挂载可继承。

## 4. 新增/建议的可选环境变量

在运行脚本的 shell 里设置。脚本把它写进新容器，以后的发布自动继承。

| 变量 | 默认值 | 建议 |
|---|---|---|
| `AI_LOG_UPSTREAM_BODIES` | `false` | 保持默认。只有系统管理员要求时才设 `true`。只接受 `true`/`false`，其他值脚本直接拒绝。 |
| `AI_LOG_SPOOL_MAX_BYTES` | `21474836480`（20 GiB） | 调到至少能扛住一天 WAN/Kafka 中断。 |

spool 容量估算：上游正文关闭后，按当前流量约每天 25–30 GB。文件系统空间够时建议 40 GiB（`42949672960`），条件是 spool 写满后该文件系统仍保留至少 20% 空闲。

```bash
df -h "$SPOOL"          # 看 Size 和 Avail
sudo du -sh "$SPOOL"    # 当前积压；目录为 0700，需要 sudo
```

判断：`Avail` + 当前积压 − 40 GiB ≥ `Size` × 20%。不满足就选更小的值，或先扩容。

值必须是字节数整数，最小 `67108864`（64 MiB）。值不合法时新容器起不来，脚本超时后删除它，旧容器继续服务。spool 写满后推理照常，但新事件会丢失，表现为第 5 节的 `ai_log_capture_degraded` 和 `wal_dropped_events`。

## 5. 验收

切换完成后，沿用 3.1 的 `STATE` 和 `SPOOL`：

```bash
NEW=$(sed -n 's/^active_container=//p' "$STATE")
docker inspect "$NEW" --format '{{.Config.Image}}'   # sub2api:0.2.15-ailog.20261020
docker inspect "$NEW" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AI_LOG_'
```

**5.1 来源心跳。** 中央日志平台管理页上，该来源心跳在线。shipper 每 30 秒上报一次，120 秒收不到会显示离线。没有管理页账号的，请系统管理员确认。

**5.2 shipper 积压。** shipper 每 30 秒把状态写到 spool 目录下的 `shipper-status.json`，并在日志里打一行 `status`：

```bash
sudo cat "$SPOOL/shipper-status.json"; echo; date +%s
sudo jq . "$SPOOL/shipper-status.json"   # 装了 jq 时
sudo journalctl -u sub2api-ai-log-shipper --since "10 min ago" | grep ' status '
```

字段：`pending_bytes` 未发出的 WAL 字节；`oldest_age_seconds` 最老未发送段的年龄（秒）；`failures` 本轮发送失败的段数；`delivered_events` shipper 启动以来累计发送数；`updated_at` 写入时间（Unix 秒）。

正常：`updated_at` 与 `date +%s` 相差不超过约 1 分钟；`failures` 为 0；`pending_bytes`、`oldest_age_seconds` 很小，或在持续下降。

**5.3 应用日志。**

```bash
docker logs --since 10m "$NEW" 2>&1 | grep ai_log_capture_degraded
```

无输出为正常。有输出说明内存队列或 spool 已满，正在丢事件。该行最多每 30 秒一次，`dropped_events` 是容器启动以来的累计值。请检查 spool 磁盘和 shipper，并联系系统管理员。

**5.4 端到端核对。** 发一条正常的流式模型请求，自带一个 `X-Request-ID`（不超过 64 字节）。网关会把它记入 `capture.start` 的 `request_id`。

```bash
read -rs SUB2API_TEST_KEY   # 输入一把测试用 API Key，不回显
RID="relay-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"; echo "$RID"; date -u
curl -sS -N -D - https://<中转站域名>/v1/messages \
  -H "x-api-key: $SUB2API_TEST_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -H "X-Request-ID: $RID" \
  -d '{"model":"<该 Key 分组可用的模型>","max_tokens":32,"stream":true,"messages":[{"role":"user","content":"ping"}]}'
unset SUB2API_TEST_KEY
```

示例是 Anthropic 格式，也可以用平时客户端所用的接口，同样带上 `X-Request-ID`。把 `RID`、UTC 时间和响应头里的 `x-request-id` 发给系统管理员；个别上游路径会改写响应头，以你发送的 `RID` 为准。由中央核对：该请求已完整入库，`capture.start` 带 `upstream_bodies=false`，`capture.end` 没有 `wal_dropped_events`。

## 6. 凭据轮换提醒

当前 g2 Kafka 凭据按 90 天轮换，约 2027-01-07 前需要轮换。到期规则只做提醒，不会自动吊销。

轮换由系统管理员在中央日志平台发起，并提供新的私有 shipper 配置。届时才按 [AI_LOGGING.md](AI_LOGGING.md) 重跑安装器：

```bash
sudo deploy/install-ai-log-shipper.sh /absolute/private/shipper.json
```

安装器拒绝原地更改 source、topic、spool，不会清空 WAL；新配置启动失败时自动恢复上一份配置。本次升级与轮换无关，不要提前执行。

## 7. 联系

有问题、发现异常，或需要开启上游正文，请联系系统管理员。

发给系统管理员的内容只包括：来源 ID（`AI_LOG_SOURCE_ID`）、镜像 tag、时间、`RID`、`shipper-status.json` 内容和相关日志行。不要发送 `shipper.json`、API Key 或任何密码。
