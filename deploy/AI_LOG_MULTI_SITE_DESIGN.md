# Sub2API 多站点日志接入管理设计

状态：待实施方案，2026-10-09。当前分支已有 WAL、Kafka 上报和日志后端；本方案中的中央注册页、自动凭据管理、来源绑定和心跳功能尚未实现。两个部署合计每天 50–100 GB 压缩前日志，共用现有 Kafka、清洗、ClickHouse 和 HDD R6 归档。

采用中央注册、每个部署独立身份和凭据、每个来源独立 raw Topic。清洗后汇入同一个组织的 clean Topic，保留来源字段；管理员可以合并查询或按站点筛选。两个站点运行同一份 Sub2API 镜像，差异只在接入配置。

[可编辑架构图](ai-log-multi-site.drawio)。

## 使用方式

中央日志平台增加“来源接入”页面，拟用现有日志域名的 `/admin/sources` 路径；该路径尚未开放。管理员通过内网或 VPN 和管理员身份登录，分别创建“站点 A”“站点 B”。名称和地域只是显示信息，系统生成不可变的 source_id。

创建表单包含名称、环境、地域标签、预计日流量、告警阈值。后台完成 Topic、账号、ACL、配额和清洗绑定后，页面才显示“可安装”，提供该来源独有的私密 `shipper.json` 配置包及安装说明。第一版使用配置包安装，避免让浏览器直接管理宿主机服务；后续再增加一次性接入码的一键注册。

在各站点宿主机安装 shipper，挂载该站点本地 WAL，并把生成的 source_id 配置给 Sub2API。两地使用同一套安装脚本和代码；不能复制共用密码，也不跨地点共享 WAL。一个站点的蓝绿容器共享该站点的 source_id 和本地 WAL，仍由一个 shipper 发送。

接入管理页展示“配置已生成、已收到心跳、Kafka 测试通过、已归档验证”四个独立进度。只有专用合成事件已经从 HDD 回放核对，才标为“已接通”；仅有心跳或 TCP 连接不能证明日志存储成功。

| 项目 | 站点 A 示例 | 站点 B 示例 |
|---|---|---|
| 显示名称 | 站点 A | 站点 B |
| 所属组织 | default | default |
| 固定 source_id | src-a1 | src-b1 |
| Kafka 用户 | ingest-src-a1-g1 | ingest-src-b1-g1 |
| Kafka 密码 | 单独随机生成 | 单独随机生成 |
| 原始 Topic | ai.raw.src-a1.v1 | ai.raw.src-b1.v1 |
| 清洗后 Topic | ai.clean.default.v1 | ai.clean.default.v1 |
| 心跳凭据 | A 专属，只可报 A 状态 | B 专属，只可报 B 状态 |
| 存储与查询 | 共用后端，保留 source_id | 共用后端，保留 source_id |

表中的 ID、用户名和站点名称仅为示例，不是真实创建的账号。首次生成后 source_id 不随改名或换密钥改变。未来一个地点增加独立上报机器时，为每个独立 shipper 分配凭据和来源，可用相同 site 标签分组；不要把多台机器克隆成同一个上报身份。

## 注册与状态页面

来源列表列出名称、地域、状态、最近心跳、最近 Kafka 接收、最近归档、今日原始/归档字节、WAL 积压、当前凭据代次。空闲但心跳正常与心跳失联分别显示，避免把没有业务流量当作故障。

来源详情提供连接信息、配置包领取、配置版本、测试上报结果、凭据轮换、停用上报、操作审计。界面显示凭据标识和创建时间，不回显旧密码。数据库中的 source_id 与 Topic 归属不可直接编辑；改名只改变显示标签。

Sub2API 后台可增加“系统设置 → AI 日志”状态页，展示本站 source_id、采集状态、最近上报、积压与丢失计数。首版以读取无秘密状态文件为主，源端安装和激活仍走宿主机脚本。当前 AI_LOG_* 来自启动环境，界面不得假装修改后即时生效：首次接入或修改采集开关，要走现有蓝绿发布。后续若加入在线切换，再实现明确的后端动态配置接口。

浏览器与 Sub2API 容器不持有 Kafka 管理权限，不挂 Docker socket，不通过 Web 请求执行宿主机 root 命令。配置包只供 shipper 使用，上游模型 API Key、Sub2API 用户 Key、Kafka 上报凭据是三个独立用途。

## 身份与权限

Kafka 使用现有 SASL_SSL 和 SCRAM-SHA-512，三个 broker 的 443 地址不变。每个凭据代次只对所属 raw Topic 授予必要的 Write/Describe，允许非事务幂等生产所需权限；不给 Create、Delete、Alter、Read、消费者组权限，不给 clean、DLQ 或 receipts 写权限。账号与 Topic 权限由中央后台通过 Kafka Admin API 配置。[Kafka ACL 说明](https://kafka.apache.org/41/security/authorization-and-acls/)

消费者收到的普通消息体不能作为发送者身份凭证。`client.id`、payload.source_id、地域标签都可由客户端填写。因此必须形成服务端可信链：

`SCRAM 用户 → 精确 Topic ACL → raw Topic 注册绑定 → tenant_id + source_id`

清洗器的配置从单一 tenant 字符串升级为显式绑定：

```json
{
  "binding_version": 2,
  "raw_topics": {
    "ai.raw.src-a1.v1": {"tenant_id": "default", "source_id": "src-a1"},
    "ai.raw.src-b1.v1": {"tenant_id": "default", "source_id": "src-b1"}
  }
}
```

清洗器先按 Topic 取预期来源，再校验消息声明的 source_id；不一致则进入 DLQ 并告警，不能让 A 写出的消息冒充 B，也不能直接重写 ID 后悄悄接受。合格事件由服务端写入绑定身份，同时增加 raw_topic、raw_partition、raw_offset 作为追溯位置。未登记的 Topic 不订阅、不自动接纳。

两地属于同一组织，所以共享 `tenant_id=default`；不同 source_id 用于身份、计量和运维。上报账号和心跳 token 都没有查询日志的权限，中央查询凭据不能放进站点配置包。

当前归档包可以混合多个 source，`GET /v1/archive/{key}` 返回整个包。首版只让该组织中央管理员查询全部来源。将来若提供“站点仅查本站”的角色，必须同时加 SQL 和回放来源授权，并禁止直接下载含其他来源的原包，改为过滤导出；仅加一个 source_id 筛选框不构成权限隔离。

## 凭据与配置生命周期

每个来源拥有两类长期凭据：Kafka SCRAM 用户/密码用于写日志；独立的 source token 只用于本站心跳和配置确认，不得写 Kafka、读日志、创建来源或生成凭据。管理员登录凭据另外管理。

Kafka 密码采用高熵随机值；短时待交付材料加密存储，密钥位于后台主机的受限配置中，与数据库分别管理。配置包领取有短有效期，禁止进入访问日志、请求日志或审计正文。领取窗口结束后清除待交付明文/密文材料；旧密码丢失走轮换流程。source token 仅存摘要用于校验。不要把密码放在 URL、命令行参数或终端输出中。

轮换采用两代不同用户名：

1. 创建 `ingest-src-a1-g2` 及新随机密码，仍绑定 A 的同一个 raw Topic。g1 保持可用。
2. 生成新配置包；安装器原子替换受限配置，要求 source_id、Topic 和本地 WAL 路径保持不变。新配置校验失败则保留旧配置。
3. shipper 切换 Producer，等待旧批次确认；结果不确定的 WAL 保留并重试。稳定 event_id 处理可能的重复，不清空 spool。
4. 从源端使用新配置发送合成事件并收到 broker 确认，后台验证归档；首版由管理员完成“确认切换”。配置领取或普通心跳不能单独作为切换完成证据。
5. 确认后撤销 g1 的写 ACL，再删除 g1 SCRAM 记录。默认 24 小时过渡窗口，未确认则告警并保留可见的待处理状态，不默默认为轮换成功。紧急泄漏时立即撤销旧权限，不等待平滑切换。

采用独立用户名是为了允许新旧凭据重叠。Kafka 文档明确，更新 SCRAM 凭据用于后续新连接；不能仅凭修改密码就认定旧连接已断开。[Kafka SCRAM 说明](https://kafka.apache.org/41/security/authentication-using-sasl/)

“停用上报”撤销该来源所有凭据代次的写权限，验证已有连接与新连接均不能写；保留历史数据和来源目录。源端会积压 WAL，页面必须显示这个影响。正常退役应先停止采集、排空 WAL、核对最后归档，再撤权；不能用删除来源来删除历史日志。

首版建议 90 天轮换提醒，轮换动作与结果均入审计。SCRAM 本身没有本方案的自动到期策略；需要后台任务执行撤权，不能只在数据库里标记 expired。

## 上报配置与心跳

现有 shipper.json 增加可选的 control 部分，其余数据通道保持兼容：

```json
{
  "bootstrap_servers": "broker-1.kafka.infra.qingtianji.com:443,broker-2.kafka.infra.qingtianji.com:443,broker-3.kafka.infra.qingtianji.com:443",
  "username": "ingest-src-a1-g1",
  "password": "GENERATED_SECRET",
  "topic": "ai.raw.src-a1.v1",
  "source_id": "src-a1",
  "spool_dir": "/var/lib/sub2api-ai-log/spool",
  "config_version": 1,
  "credential_id": "cred-a1-g1",
  "control": {
    "endpoint": "https://logs.kafka.infra.qingtianji.com",
    "source_token": "GENERATED_SCOPED_TOKEN"
  }
}
```

控制接口为拟新增功能。心跳每 30 秒一次、超过 120 秒标为失联，携带版本、配置代次、启动实例标识、pending_bytes、oldest_age、投递成功时间和丢失计数，不携带消息正文或秘密。token 在服务端绑定 source，不接受请求体指定其他来源。心跳使用独立、有限超时的循环，不能被 Producer 的长重试阻塞。

后台同时维护服务器观察的 raw 接收时间、clean 接收时间和归档确认时间。源端报告用于诊断，不能作为计费或身份事实；后台数据才证明日志已进入链路。

管理页或注册库不可用时，已有站点继续使用最后可用的本地 Kafka 配置上报。控制故障不能同步阻塞推理或 WAL。首版不上通用“远程执行命令”能力，只允许上报状态、确认配置和专门的合成探测。

## 中央服务和数据库

在现有 VM624 增加独立 `ai-log-control` 服务，与只读查询 API 分进程、分账号。Web 服务接受管理员操作并写作业；只有内网 provisioner 持有 Kafka 管理客户端证书，不能将其交给公网实例。管理操作必须受管理员认证、CSRF 防护、审计和内网/VPN访问限制；不能复用当前查询 bearer token 作为管理权限。

两处部署的注册元数据使用 SQLite 足够，放在 VM624 系统盘的专用目录 `/var/lib/ai-log-control/`，使用 WAL + FULL 同步及在线备份。这里只保存少量配置和作业，日志正文仍在 HDD，日志检索仍在 ClickHouse。将来需要多实例控制服务时再迁 PostgreSQL，不为两个来源新增数据库 VM。

| 表 | 主要用途 |
|---|---|
| sources | source_id、tenant、名称、地域、raw Topic、期望状态、配额、配置版本 |
| credentials | source_id、principal、代次、状态、创建/撤销/确认时间、短时加密交付材料 |
| source_tokens | token 摘要、source_id、权限范围、状态、最近使用 |
| config_versions | 非秘密配置、版本、摘要、确认状态；秘密通过 credential_id 引用 |
| provision_jobs | 幂等键、步骤、期望/实测状态、错误类别、重试信息 |
| audit_events | 操作者、目标、动作、结果、时间；无密码或日志正文 |

高频心跳写入来源最新状态，长期趋势进入 Prometheus，避免在 SQLite 无限追加每 30 秒一条记录。注册库与解密密钥分别备份，恢复时按数据库期望状态与 Kafka 实际资源对账；不能恢复后无条件重建账号或改密码。

注册不是一个跨数据库和 Kafka 的原子事务。实现 `provisioning → ready → active`，失败显示 pending/error，后台作业以稳定 resource ID 重试和读回：

1. 事务写入来源与 provisioning 作业，冻结 source_id/topic。
2. 创建并检查 Topic 的分区、RF、保留参数，创建凭据与精确 ACL、用户配额。
3. 写入版本化来源绑定；清洗器验证配置后在事务边界切换订阅。首版可以受控重启清洗进程，Kafka 暂存积压；不能重启整个 Kafka 集群。
4. 确认清洗绑定已生效后，再允许下载并激活来源配置。
5. 身份未生效、Topic 创建失败、权限未传播等情况不能显示 active。

如果暂停接收新来源，已有积压仍需按原身份清洗/归档。registry 的 enabled 状态不能让已经确认收到的合法记录被忽略。

拟新增接口如下，名称用于实现评审，当前不存在：

| 接口 | 授权和行为 |
|---|---|
| POST /control/v1/sources | 管理员创建来源；要求 Idempotency-Key |
| GET /control/v1/sources | 管理员查看状态与用量 |
| GET /control/v1/sources/{id}/bundle | 管理员领取该作业配置包，短期授权，禁止缓存/日志正文 |
| POST /control/v1/sources/{id}/rotations | 管理员创建下一代凭据与配置 |
| POST /control/v1/sources/{id}/rotations/{rid}/confirm | 新配置测试与归档完成后确认撤销旧凭据 |
| POST /control/v1/sources/{id}/disable | 撤销写权限，不删除日志 |
| POST /agent/v1/heartbeat | source token 只能报告自身状态 |
| POST /agent/v1/config-acks | source token 确认本站已应用的配置摘要；不单独触发撤销 |

管理路径只能经受限入口访问，agent 路径经现有 HTTPS 443 访问；Kafka 数据流继续走三个 broker 的 SASL/TLS 443，不需要再开放 WAN 端口。

## 一次性接入码的后续扩展

第二阶段把下载配置包替换为“生成 15 分钟接入码 → 宿主机交互式输入 → 领取并安装配置”。安装程序先在本地生成设备密钥和持久化 request_id，后台把接入码绑定到来源及设备公钥，重试需证明同一设备私钥，避免网络超时后重复注册。一个接入码不能注册第二个来源或第二台设备。

接入码只负责首次激活，不能充当长期 Kafka 密码。后台只存接入码摘要，验证尝试限速。一次性绑定与配置领取分阶段确认，响应丢失时允许同一设备、同一 request_id 在领取窗口恢复；不能简单“先烧掉 token，再返回密码”导致半注册。

管理页不在线不影响已有 Kafka 上报。自动配置拉取、平滑自动轮换与 Sub2API 页面一键注册以这个设备身份为基础，不能在没有宿主机 agent 的情况下，靠容器页面直接修改宿主机文件。

## Topic 与容量

每个 raw Topic 初期仍用 6 分区，与现有 clean 的 6 分区保持一致，RF=3、minISR=2；清洗器保留按分区顺序发往 clean 的行为。来源增长后再按实测重平衡，不在第一版改变序号与分区契约。

**两个站点合计 50–100 GB/天，不能把每站点的容量额度当成总量再翻倍。** 保持原 raw 总字节预算 384 GiB/副本，初期平均分配 A/B：每来源 192 GiB，即 32 GiB/分区；clean 仍为 42 GiB × 6 分区。配额按实际流量调整，比如 A/B 为 80/20，而不是给每个新来源复制一份完整预算。72 小时与字节上限先到者生效；偏斜分区可能更早淘汰。

旧 raw.default 迁移期间占用也计入总预算。当前公共 default 接入账号仅用于既有验收，生产两站接入前需确认没有其他使用者，再撤销其写权限；保留并归档原消息。新来源不得继续使用共享 ingest-default。不能在未核对旧消费者和归档之前直接删除旧 Topic 或重置位点。旧归档保留原有 source_id，并在迁移目录中标记为 legacy；不能把原来由消息自报的来源追认为已经通过新身份绑定验证。

Kafka 用户级速率配额按来源设置，避免一个来源压垮另一个；client.id 只作标签，不作为可信配额身份。配额是 broker 侧约束，不等于全群总速率；轮换两代账号并存时要计入合计预算。第一版管理页显示限速状态与 WAL 增长，不宣称达到某个尚未实测的公网峰值。

总量不变，既有 HDD 估算仍成立：最终落盘为原始量 10% 时约 1.83–3.65 TB/年，25% 时约 4.56–9.13 TB/年。需计入中转双边界和重试带来的采集放大。两处来源不要求两份正文数据库；正文独立第二副本仍是已有待解决项，新增接入管理不会自动补上备份。

## 实施顺序和验收

第一批实现中央来源页、注册库、幂等 provisioner、每来源 raw Topic 和凭据、清洗身份绑定、私密配置包、心跳、手动平滑轮换。沿用现有部署分支，在功能完成和验收前保持多站点新流程关闭。

第二批增加 Sub2API 状态页、一次性接入码、设备绑定与自动配置轮换。来源隔离和独立凭据应在第一批完成，不能等 UI 自动化后才补。

必须验证以下结果：

- A/B 同时写入，各自完整回放；两者使用相同 event_id 或 trace_id 时，跨来源事件不会被误去重。相同 trace 可跨站汇总，每个 capture 的序号独立，不能按跨站时间戳推断全局因果顺序。
- A 的密码不能写 B Topic、不能写 clean、不能读 Topic；A 在自己 Topic 中伪造 B source_id 被隔离到 DLQ。
- A source token 不能替 B 发心跳、领配置或查日志；查询入口的来源筛选与授权边界符合上述限制。
- A 撤权或密码泄漏处理不影响 B；撤权后既有连接与新连接写入都被拒绝。
- g1/g2 平滑切换时不删除未确认 WAL、不改变 source_id，重复事件正确去重；未完成轮换保持可见待处理状态。
- 注册过程中 Topic 创建/ACL/绑定发布任一步失败，重复同一幂等请求不会生成第二来源或乱换密码。
- 中央管理服务不可用时，A/B 均继续上报；心跳线程失效不会阻塞 WAL 投递。
- 浏览器、日志、Git、审计记录中不出现凭据正文；首个合成请求最终能从 HDD 精确还原。
