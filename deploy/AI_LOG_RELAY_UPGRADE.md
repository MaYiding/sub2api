# 中转站 AI 日志采集升级指南：v0.2.15-ailog.20261021

面向运行本分支、已开启 AI 日志采集的外部中转站运维人员。
前提：站点已按 [BLUE_GREEN.md](BLUE_GREEN.md) 做过蓝绿发布，shipper 已按 [AI_LOGGING.md](AI_LOGGING.md) 安装并在运行。不用蓝绿、直接用 docker compose 运行应用的站点见附录 A。

**本指南可以重复执行。** 每一步都先检查，已经做过的就跳过。中途断开（SSH 掉线、换人接手）后，从第 0 节重新开始即可：不会重复切换，也不会删掉回滚要用的旧容器。

## 0. 先确认当前状态（只读）

在中转站上，用平时发布所用的账号执行（设置过 `SUB2API_DEPLOY_DIR` 的按实际目录改）：

```bash
TARGET=sub2api:0.2.15-ailog.20261021
STATE=/opt/sub2api/deploy/.blue-green-active
cat "$STATE"
ACTIVE=$(sed -n 's/^active_container=//p' "$STATE")
docker inspect "$ACTIVE" --format 'active={{.Config.Image}} id={{.Image}}'
docker run --rm --entrypoint /app/sub2api "$(docker inspect "$ACTIVE" --format '{{.Image}}')" --version 2>&1 | head -1
docker image inspect "$TARGET" --format 'target id={{.Id}}' 2>/dev/null || echo '目标镜像还不在本机'
```

- 版本输出含 `0.2.15-ailog.20261021`，或 `active` 那行的 id 与 `target id` 相同：已经升级完成，直接做第 5 节验收。
- 其他情况按第 2、3 节升级。版本低于 `0.2.15-ailog.20261020` 的站点也直接升到本版本，不用先部署中间版本；第 1 节列出的两组变更会同时生效。

## 1. 适用范围与变更摘要

tag `v0.2.15-ailog.20261021`（附注 tag，位于 `dev` 分支）。本次只换应用镜像和蓝绿脚本：

- 没有数据库迁移；
- 不需要新密钥，也不需要新配置；
- shipper（`tools/ai-log-pipeline/shipper.py`、`deploy/install-ai-log-shipper.sh`）没有改动，不要重跑安装器。

相对 `v0.2.15-ailog.20261020`：

1. **修复 access 日志状态码。** 开启采集后，网关 access 日志（`http request completed`）里每个被采集请求的 `status_code` 都记成 0。AI 日志里的状态码一直是对的，不受影响。
2. **`deploy/blue-green-deploy.sh` 重复执行不再有副作用。** 当前活动容器已经在运行目标镜像时，脚本打印 `already runs …; nothing to do` 后以 0 退出：不切换槽位，不改 Caddy，也不碰已停止的旧容器。确实要用同一个镜像重新发布（例如只改 spool 上限）时，加 `SUB2API_REDEPLOY_SAME_IMAGE=true`。
3. 会话合并（`tools/ai-log-pipeline` 中的会话服务）的修复只部署在中央，中转站不用做任何操作。

还没升到 `v0.2.15-ailog.20261020` 的站点，本次还会一并得到下面这些变更：

4. `capture.end` 新增 `wal_dropped_events`，即 WAL 写入器已接收、但没能写入磁盘的事件数（例如 spool 已满）。这个字段只在非零时出现。以前这类丢失在单个请求上看不到。
5. 记录 Claude Code 的 `X-Claude-Code-Session-Id`，并优先用它作为 trace ID；`X-Claude-Code-Agent-Id`、`X-Claude-Code-Parent-Agent-Id` 一并保留。
6. **默认行为变化：上游尝试的请求和响应正文默认不再复制。** 新增可选变量 `AI_LOG_UPSTREAM_BODIES=true|false`，默认 `false`。关闭时，每次上游尝试仍记录状态码、字节数和 sha256。上游正文约占采集总量的一半，所以关闭后 WAN、Kafka 和本地 spool 的负载大约减半。这是缓解 spool 写满的主要手段。
7. 蓝绿脚本会像其他 `AI_LOG_*` 设置一样，把 `AI_LOG_UPSTREAM_BODIES` 带到新容器。

## 2. 构建镜像并送到中转站

在单独的构建机上构建，不要在公网生产中转站上构建。构建机需要 git，以及支持 BuildKit 的 Docker（Dockerfile 里用了 `RUN --mount`）。

```bash
[ -d sub2api/.git ] || git clone https://github.com/MaYiding/sub2api.git
cd sub2api
git fetch --tags origin
git checkout v0.2.15-ailog.20261021
git describe --tags --exact-match   # 应输出 v0.2.15-ailog.20261021
git rev-parse HEAD                  # 应输出 49e0233a1bf46de7d703a756b098ca224c267e81
```

`.dockerignore` 排除了 `.git`，构建时读不到 tag，版本会退回 `backend/cmd/server/VERSION` 里的 `0.2.15`。所以版本号和提交号要用构建参数传入。另外，镜像上会打一个提交号标签，再次执行时，已经从同一提交构建过的镜像会被跳过：

```bash
IMG=sub2api:0.2.15-ailog.20261021
REV=$(git rev-parse HEAD)
if [ "$(docker image inspect "$IMG" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = "$REV" ]; then
  echo "已从 $REV 构建过，跳过"
else
  docker build --platform linux/amd64 -f Dockerfile \
    --build-arg VERSION=0.2.15-ailog.20261021 \
    --build-arg COMMIT="$(git rev-parse --short HEAD)" \
    --label org.opencontainers.image.revision="$REV" \
    -t "$IMG" .
fi
```

- `DATE` 不传时取构建时间。Dockerfile 默认 `GOPROXY=https://goproxy.cn,direct`；需要别的代理时，加 `--build-arg GOPROXY=...`（写法见 `deploy/build_image.sh`）。
- 蓝绿脚本只接受 `linux/amd64` 镜像。在 ARM 机器上构建，也要保留 `--platform linux/amd64`。

把镜像送到中转站。可以推到你们自己的私有镜像仓库，再在中转站上拉取（蓝绿脚本本身不会拉镜像）；也可以不经仓库直接传，这要求远端账号能执行 docker。先比较两边的镜像 ID，相同就不用再传：

```bash
docker image inspect "$IMG" --format '{{.Id}}'
ssh <中转站主机> "docker image inspect $IMG --format '{{.Id}}'"   # 报错或与上一行不同时，才执行下一行
docker save "$IMG" | gzip | ssh <中转站主机> 'gunzip | docker load'
```

在中转站上自检：

```bash
docker image inspect sub2api:0.2.15-ailog.20261021 --format '{{.Os}}/{{.Architecture}}'   # linux/amd64
docker run --rm --entrypoint /app/sub2api sub2api:0.2.15-ailog.20261021 --version 2>&1      # 含 0.2.15-ailog.20261021
```

## 3. 蓝绿发布

注意事项：

- 现有 g2 shipper 配置 `/etc/sub2api-ai-log/shipper.json`（权限 0600）不动。它含 Kafka 凭据，不要查看、复制或外发。
- `AI_LOG_ENABLED`、`AI_LOG_SOURCE_ID`、`AI_LOG_SPOOL_MAX_BYTES`、`AI_LOG_UPSTREAM_BODIES` 和 `/app/ai-log-spool` 挂载，由脚本从当前运行的容器继承。
- 不要重跑 `install-ai-log-shipper.sh`，不要改 shipper 配置，不要清空或移动 spool 目录。积压的数据要保留，shipper 会继续发送。
- 切换期间新旧容器会短暂同时写同一个 spool，这是设计行为。

### 3.1 发布前确认

沿用第 0 节的 `STATE` 和 `ACTIVE`。

**记录升级前的镜像。** 记录文件只在第一次执行时写入，以后重复执行不会把它覆盖成新版本：

```bash
BEFORE="$(dirname "$STATE")/ailog-upgrade-20261021.before"
[ -s "$BEFORE" ] || docker inspect "$ACTIVE" --format 'container={{.Name}} image={{.Config.Image}} id={{.Image}}' > "$BEFORE"
cat "$BEFORE"   # 回滚时用
```

**检查采集设置和 spool 目录：**

```bash
docker inspect "$ACTIVE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AI_LOG_'
SPOOL=$(docker inspect "$ACTIVE" --format '{{range .Mounts}}{{if eq .Destination "/app/ai-log-spool"}}{{.Source}}{{end}}{{end}}')
echo "$SPOOL"   # spool 的宿主机目录
docker ps -a --filter name=sub2api --format '{{.Names}}  {{.Status}}  {{.Image}}'
```

应看到 `AI_LOG_ENABLED=true`、`AI_LOG_SOURCE_ID=...`，并且 `SPOOL` 不为空。不符合就先停下，联系系统管理员。

**检查目标槽位。** 当前活动容器是 `sub2api-green` 时，目标槽位是 `sub2api-blue`；其他情况（`sub2api-blue`，或者没有状态文件时的旧容器 `sub2api`）目标槽位是 `sub2api-green`：

```bash
SLOT=$([ "$ACTIVE" = sub2api-green ] && echo sub2api-blue || echo sub2api-green)
docker inspect "$SLOT" --format '{{.Name}} {{.State.Status}} {{.Config.Image}}' 2>/dev/null || echo "$SLOT 空闲"
```

脚本要求目标槽位上没有容器。那里通常停着上上次发布留下的旧容器，状态为 `exited`。确认它不是 `$ACTIVE` 后删除（它的数据目录会保留）：`docker rm "$SLOT"`。槽位已空时跳过这一步。当前在运行的版本升级后会留在另一个槽位，作为这次的回滚点。

### 3.2 替换脚本

中转站上要用本 tag 的 `deploy/blue-green-deploy.sh`。旧脚本不会传递 `AI_LOG_UPSTREAM_BODIES`，重复执行时也会再切一次槽位。先在中转站上比较哈希值：

```bash
sha256sum <原脚本所在目录>/blue-green-deploy.sh
# 本 tag 的脚本：9cd934329564cafc89c980aa51427c41cae598f4c7553be4d635c47d16ec0454
```

哈希值不同时，从构建机复制（`-p` 保留可执行权限）：

```bash
scp -p deploy/blue-green-deploy.sh <中转站主机>:<原脚本所在目录>/blue-green-deploy.sh
```

### 3.3 执行

在中转站上运行。`SUB2API_CADDY_SITE_HOST` 每次都必须设置，Caddy 相关变量与上次发布保持一致：

```bash
export SUB2API_CADDY_SITE_HOST='<与上次发布相同>'
# 上次改过默认值时才需要：
# export SUB2API_CADDYFILE=/opt/sub2api-https/Caddyfile
# export SUB2API_CADDY_CONTAINER=sub2api-https
./blue-green-deploy.sh sub2api:0.2.15-ailog.20261021
# 同时调大 spool 上限时（见第 4 节）：
# AI_LOG_SPOOL_MAX_BYTES=42949672960 ./blue-green-deploy.sh sub2api:0.2.15-ailog.20261021
```

脚本先检查以下各项：

- 镜像已在本机，且为 `linux/amd64`；
- Caddyfile 含 `lb_policy first`、`health_uri /health`、`stream_close_delay`；
- 开启采集时，spool 是已存在的绝对路径，不是软链，也不在应用数据目录内；
- 主机锁保证同一时间只有一个发布在跑；
- 活动容器已经在运行这个镜像时，打印 `nothing to do` 并退出。

然后脚本把 `/app/data` 复制给新容器，等 Docker 健康检查和 `/health` 都通过（默认最多等 180 秒），切换 Caddy，再观察 30 秒。任何一步失败，脚本都会恢复 Caddy、删除失败的新容器，旧容器继续服务；排除原因后从第 0 节重来即可。成功后脚本写状态文件，旧容器排空 60 秒后停止，但不删除。

### 3.4 回滚

1. 按 3.1 删除目标槽位上已停止的容器。升级成功后，这个容器就是升级前的那个，回滚会用同一个镜像在原槽位重建它。
2. 用 `$BEFORE` 里记下的镜像再跑一次脚本：

   ```bash
   cat "$BEFORE"
   ./blue-green-deploy.sh <升级前的镜像>
   ```

采集设置和 spool 挂载照常继承。比 `0.2.15-ailog.20261020` 更早的镜像不认识 `AI_LOG_UPSTREAM_BODIES`，会恢复为总是复制上游正文，spool 负载回到升级前的水平。

**只想停采集：**

```bash
AI_LOG_ENABLED=false SUB2API_REDEPLOY_SAME_IMAGE=true ./blue-green-deploy.sh <当前镜像>
```

新容器不再挂载 spool。spool 和 shipper 保留，等积压发完。以后重新启用时，要同时给出 `AI_LOG_ENABLED=true` 和 `SUB2API_AI_LOG_SPOOL_HOST_DIR=<spool 宿主机目录>`，因为停采集的容器上没有挂载可以继承。

## 4. 可选环境变量

在运行脚本的 shell 里设置。脚本会把它们写进新容器，以后的发布自动继承。

| 变量 | 默认值 | 建议 |
|---|---|---|
| `AI_LOG_UPSTREAM_BODIES` | `false` | 保持默认。只有系统管理员要求时才设为 `true`。只接受 `true`/`false`，其他值脚本直接拒绝。 |
| `AI_LOG_SPOOL_MAX_BYTES` | `21474836480`（20 GiB） | 调到至少能扛住一天的 WAN/Kafka 中断。 |
| `SUB2API_REDEPLOY_SAME_IMAGE` | `false` | 只在用当前镜像重新发布、以改变上面两个设置时才设为 `true`。只对这一次执行生效，不写进容器。 |

spool 容量估算：上游正文关闭后，按当前流量大约每天 25–30 GB。文件系统空间够时建议设为 40 GiB（`42949672960`），前提是 spool 写满后，该文件系统仍保留至少 20% 的空闲空间。

```bash
df -h "$SPOOL"          # 看 Size 和 Avail
sudo du -sh "$SPOOL"    # 当前积压；目录权限为 0700，需要 sudo
```

判断：`Avail` + 当前积压 − 40 GiB ≥ `Size` × 20%。不满足就选更小的值，或者先扩容。

值必须是以字节为单位的整数，最小 `67108864`（64 MiB）。值不合法时新容器起不来，脚本超时后会删除它，旧容器继续服务。spool 写满后推理照常进行，但新事件会丢失，表现为第 5 节里的 `ai_log_capture_degraded` 和 `wal_dropped_events`。

## 5. 验收

切换完成后，沿用前面的 `STATE` 和 `SPOOL`：

```bash
NEW=$(sed -n 's/^active_container=//p' "$STATE")
docker inspect "$NEW" --format '{{.Config.Image}}'   # sub2api:0.2.15-ailog.20261021
docker inspect "$NEW" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AI_LOG_'
```

**5.1 来源心跳。** 在中央日志平台的管理页上，确认该来源的心跳在线。shipper 每 30 秒上报一次，120 秒收不到就显示离线。没有管理页账号的，请系统管理员确认。

**5.2 shipper 积压。** shipper 每 30 秒把状态写到 spool 目录下的 `shipper-status.json`，并在日志里打一行 `status`：

```bash
sudo cat "$SPOOL/shipper-status.json"; echo; date +%s
sudo jq . "$SPOOL/shipper-status.json"   # 装了 jq 时
sudo journalctl -u sub2api-ai-log-shipper --since "10 min ago" | grep ' status '
```

字段含义：

| 字段 | 含义 |
|---|---|
| `pending_bytes` | 还没发出的 WAL 字节数 |
| `oldest_age_seconds` | 最老的未发送段已存在多少秒 |
| `failures` | 本轮发送失败的段数 |
| `delivered_events` | shipper 启动以来累计发送的事件数 |
| `updated_at` | 写入时间（Unix 秒） |

正常情况：`updated_at` 与 `date +%s` 相差不超过约 1 分钟；`failures` 为 0；`pending_bytes` 和 `oldest_age_seconds` 很小，或者在持续下降。

**5.3 应用日志。**

```bash
docker logs --since 10m "$NEW" 2>&1 | grep ai_log_capture_degraded
```

没有输出才正常。有输出说明内存队列或 spool 已满，正在丢事件。这行日志最多每 30 秒出现一次，其中 `dropped_events` 是容器启动以来的累计值。请检查 spool 所在磁盘和 shipper，并联系系统管理员。

**5.4 端到端核对。** 发一条正常的流式模型请求，带上自己生成的 `X-Request-ID`（1–64 字节；为空或超长时，网关会换成随机 UUID，就只能按时间去找了）。网关会把它记进 access 日志和 `capture.start` 的 `request_id`。

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
docker logs --since 10m "$NEW" 2>&1 | grep 'http request completed' | grep "$RID" | grep -o '"status_code": [0-9]*'
# 应为 "status_code": 200；升级前这里是 0
```

示例用的是 Anthropic 格式，也可以改用平时客户端所用的接口，同样带上 `X-Request-ID`。

把 `RID`、UTC 时间和响应头里的 `x-request-id` 发给系统管理员。个别上游路径会改写响应头，此时以你发送的 `RID` 为准。中央会核对三点：

- 该请求已完整入库；
- `capture.start` 带 `upstream_bodies=false`；
- `capture.end` 没有 `wal_dropped_events`。

## 6. 凭据轮换提醒

当前 g2 Kafka 凭据按 90 天轮换，约 2027-01-07 前需要轮换。到期规则只做提醒，不会自动吊销。

轮换由系统管理员在中央日志平台发起，并提供新的私有 shipper 配置。届时才按 [AI_LOGGING.md](AI_LOGGING.md) 重跑安装器：

```bash
sudo deploy/install-ai-log-shipper.sh /absolute/private/shipper.json
```

安装器拒绝原地更改 source、topic 和 spool，也不会清空 WAL；新配置启动失败时会自动恢复上一份配置。本次升级与轮换无关，不要提前执行。

## 7. 联系

有问题、发现异常，或者需要开启上游正文，请联系系统管理员。

发给系统管理员的内容只包括：

- 来源 ID（`AI_LOG_SOURCE_ID`）；
- 镜像 tag；
- 时间；
- `RID`；
- `shipper-status.json` 的内容；
- 相关的日志行。

不要发送 `shipper.json`、API Key 或任何密码。

## 附录 A：没有蓝绿、用 docker compose 运行应用的站点

适用于应用直接由 `docker compose` 管理、前面没有蓝绿槽位的站点。换镜像要重建应用容器，期间约有 10 秒不可用，正在进行的流式请求会被中断，所以请选低流量时段。数据库和 Redis 不动；本版本与此前各 ailog 版本之间没有数据库迁移。

1. 第 2 节照做：构建镜像，并送到这台主机。
2. 在 compose 目录里记下当前镜像（只在第一次执行时写入），并备份定义应用镜像的文件，例如 `docker-compose.override.yml`：

   ```bash
   APP=<应用服务名>
   CID=$(docker compose ps -q "$APP")
   [ -s ailog-upgrade-20261021.before ] || docker inspect "$CID" --format 'image={{.Config.Image}} id={{.Image}}' > ailog-upgrade-20261021.before
   [ -e docker-compose.override.yml.pre-20261021 ] || cp -p docker-compose.override.yml docker-compose.override.yml.pre-20261021
   ```

3. 把应用服务的 `image:` 改成 `sub2api:0.2.15-ailog.20261021`。在它的 `environment` 里写明 `AI_LOG_UPSTREAM_BODIES=false`，已经有这一项就不要重复添加。`AI_LOG_*` 的其他设置和 spool 卷保持不变。
4. 执行下面的命令。配置和镜像都没变时，compose 不会重建容器，所以重复执行没有影响：

   ```bash
   docker compose config --quiet && docker compose up -d "$APP"
   ```

5. 按第 5 节验收，把其中的 `$NEW` 换成 `$(docker compose ps -q "$APP")`。

回滚：用 `.pre-20261021` 备份还原那个文件，再执行 `docker compose up -d "$APP"`。
