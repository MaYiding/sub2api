# Sub2API 多来源日志接入

2026-10-09：第一阶段已实现，中央服务部署在 VM624。两个部署合计每天 50–100 GB 压缩前日志，共用 Kafka、清洗服务、ClickHouse 和 HDD R6 归档。公网 Sub2API 的实际安装和蓝绿发布仍需按各站点配置执行。

入口为 `https://logs.kafka.infra.qingtianji.com/admin/sources`，需要内网或 VPN，并使用独立的日志平台管理员账号。两地运行同一个分支；差异是 source_id、Kafka 账号、原始 Topic 和心跳 token。历史[架构参考图](ai-log-multi-site.drawio)保留供编辑。

## 注册与接入

1. 管理员登记名称、地域备注、环境和原始日志缓冲额度。系统生成固定的 `src-<12 位随机十六进制>` 来源 ID。
2. 后台建立 6 分区、RF=3、minISR=2 的独立 raw Topic，生成 SCRAM-SHA-512 账号，配置精确 ACL、发送速率配额和清洗绑定。
3. 清洗器确认加载绑定后，配置包才可领取。并发注册时，尚未建好 Topic/ACL 的来源不会进入订阅集合。
4. 下载本站的 `shipper.json`，设置 0600 权限，在对应宿主机执行 `sudo deploy/install-ai-log-shipper.sh /absolute/private/shipper.json`。
5. 按安装器输出给 Sub2API 配置 `AI_LOG_SOURCE_ID`，启用 `AI_LOG_ENABLED=true` 并挂载共享本地 WAL，再执行[蓝绿发布](AI_LOGGING.md)。
6. 检查实际站点心跳、发送确认及 HDD 回放，然后用真实业务请求验收。

一个独立 shipper 对应一个来源。单台服务器的蓝绿容器共享该来源 ID 和本地 WAL；不同服务器不得共用配置包或 WAL。改名、换凭据、停用后重新启用都保持 source_id 和 Topic 不变。

页面提供注册、状态、配置下载、改名、生成下一代凭据、确认或取消轮换、停用、重新启用以及操作审计。心跳在线、源端 Kafka 发送确认和中央 HDD 回放验证分别展示。已验证表示该凭据曾完成端到端探测，**不表示此刻在线，也不证明公网生产实例已经发布**；需同时核对心跳及上报主机。

## 消费者如何判定来源

普通 Kafka 消息不会自动携带其生产者认证身份。客户端声明的 `source_id` 或 `client.id` 不能独立作为证明。实际可信链是：

`SCRAM 用户 → 精确 raw Topic ACL → 中央来源绑定 → 清洗事件来源字段`

账号只能写自己的 `ai.raw.src-<id>.v1`，不能创建或读取 Topic，不能写其他来源、clean、DLQ 或 receipts。权限通过 Kafka 自带管理工具执行，管理证书留在内网 broker 的受限 helper 上。[Kafka ACL 文档](https://kafka.apache.org/41/security/authorization-and-acls/)

清洗服务校验请求声明的 source_id 与注册 Topic 一致；不一致则进入 DLQ，保留待修复的原始记录。合法事件由服务端写入以下字段，忽略客户端伪造的同名来源属性：

| 字段 | 用途 |
|---|---|
| tenant_id | 当前两处均为 default |
| source_id | 稳定的部署来源 ID，消费者按此区分服务器 |
| source_name / source_region | 该事件清洗时的中央名称、备注快照 |
| source_verified | 是否通过中央注册 Topic 绑定校验 |
| source_binding_revision | 当时的绑定版本 |
| raw_topic / raw_partition / raw_offset | 原始 Kafka 位置，供追溯 |

这些字段保存在 clean 事件、ClickHouse 明细和 HDD Parquet 包中。消费者按 `(tenant_id, source_id, event_id)` 去重；两个来源使用相同 event_id、capture_id 或 trace_id 也不会相互覆盖。名称不是身份主键，历史事件的名称不会随改名被批量重写。

老的 `ai.raw.default.v1` 保留显式 legacy 绑定，其事件标记 `source_verified=false`，不能借该 Topic 冒充已注册来源。未登记 Topic 不会自动订阅。已停用来源仍保留清洗绑定，确保此前已确认收到的积压继续归档。

查询接口支持：

- `GET /v1/traces/{trace_id}?source_id=...`
- `GET /v1/captures/{capture_id}?source_id=...`，仍需遍历 `next_after`。

查询凭据属于中央管理员，上报账号和心跳 token 均不能查询日志。`source_id` 参数是筛选条件，不是站点级授权：当前中央查询凭据可以读取该组织所有来源。`GET /v1/archive/{key}` 会返回完整混合来源包，尚未提供“站点只能读本站”的查询角色。

## 凭据生命周期

配置包包含本站 Kafka 凭据、独立心跳 token、配置摘要和固定探测事件。数据库只保存心跳 token 摘要；待交付配置用 Fernet 加密，领取窗口为准备完成后的 24 小时。窗口结束会清除交付密文，已安装凭据仍有效；丢失配置需轮换。秘密不进入访问 URL、命令行、公开状态或审计正文。

轮换流程：生成新用户名 g2 → 安装新配置 → 新凭据心跳确认 → 固定探测事件从 HDD 回放成功 → 管理员确认 → 撤销 g1 ACL 和 SCRAM。新旧账号并存期间平分本站发送配额。取消轮换会撤销新代并保留旧代；若已安装新代，先恢复宿主机旧配置。未确认不会自动撤销旧账号。

停用立即拒绝控制心跳，并由后台撤销所有代次的写 ACL、SCRAM 和用户配额。取消尚未执行的配置作业，正在执行的作业不会把停用来源重新标成可用。既有 Kafka 连接也必须因 ACL 撤销失去写权限。重新启用生成新凭据，不恢复旧密码，保留历史数据和来源 ID。

Kafka 的 SCRAM 更新主要作用于后续连接，不能把“改了密码”当作旧连接已失效的证据；因此同时撤销写 ACL，并验收旧连接和新连接。[Kafka SCRAM 文档](https://kafka.apache.org/41/security/authentication-using-sasl/)

安装器检查 source_id、Topic 和 WAL 路径不能在原安装上更换，原子替换 0600 配置并保留前一份用于启动失败恢复。凭据轮换不清空 WAL。敏感的旧备份配置在轮换确认后可由管理员清理。

## 心跳与故障行为

独立线程每 30 秒经 HTTPS 上报，网络超时 10 秒；页面在 120 秒未收到心跳时显示离线。内容包括配置代次、持久化安装实例 ID、客户端主机名、积压、投递计数和最近确认时间，不包含日志正文。客户端主机名仅供诊断，可信来源仍由服务端绑定决定。

心跳、管理页或注册库不可用不会阻塞 Kafka 上报。Kafka 不可用时保留本地 WAL；推理请求不等待网络。采集层原有内存队列/磁盘额度耗尽时仍可能丢失日志，见[采集契约](AI_LOGGING.md)，不能把已完成的探测当成零丢失承诺。

## 中央服务部署与恢复

- VM624：`ai-log-control`（ailogweb，8081）接受管理员操作和来源心跳；`ai-log-provisioner`（ailogprov）串行执行幂等作业、观察 HDD 探测并在线备份注册库。
- VM620：root 所有的 `provision_remote.py --role kafka` helper，通过强制 SSH 命令调用本机 Kafka 管理工具。密码经私有临时属性文件传递，不放入 argv。
- VM623：`--role bindings` helper 校验并原子发布来源绑定，禁止删掉历史绑定或回退版本。清洗器在 Kafka 事务之间热加载，新增来源无需重启 broker。
- Web 无 SSH helper 私钥、Kafka 管理证书或查询 token。provisioner 仅持有来源固定、禁端口转发、固定命令的两把 SSH 密钥。
- SQLite 位于 `/var/lib/ai-log-control/registry.sqlite`，使用 WAL + FULL。`sources / credentials / jobs / requests / audit / sessions` 保存小量注册数据；`topic_ready` 防止提前订阅未完成的注册。每日 PBS 系统盘备份覆盖该目录与配置，在线 SQLite 备份每小时写入 `backup/registry.sqlite`。
- 解密密钥位于 `/etc/ai-log-control/encryption.key`。数据库与密钥分文件且受权限保护；当前都随受控的加密系统盘备份保存，并非独立故障域。恢复时同时恢复密钥、核对 Kafka 实际 ACL/账号和绑定，不无条件重置现有密码。

安装依赖时使用 `requirements-linux-amd64-py312.lock` 加 `requirements-control-linux-amd64-py312.lock`；CI/远程测试使用 `requirements-test-linux-amd64-py312.lock`。全部限定预编译 wheel 和 SHA256。

管理员使用独立密码或专用管理 bearer token；浏览器登录带 Secure/HttpOnly/SameSite cookie，修改和配置下载检查 Origin 与 CSRF。Nginx 和应用都限制管理入口为内网/VPN，公开的 `/agent/` 另有请求限速。当前 proxy HA 配置仅在持有 `.52` 的节点修改，再执行 `proxy-sync`，不要登录漂移 VIP 猜测 SSH 主机身份。

| 接口 | 行为 |
|---|---|
| POST /control/v1/login；GET /control/v1/session；POST /control/v1/logout | 管理员浏览器会话 |
| POST /control/v1/sources；GET /control/v1/sources | 创建与查看来源 |
| POST /control/v1/sources/{id}/bundle | 领取短期配置包，禁止缓存 |
| POST /control/v1/sources/{id}/rotations | 创建下一代 |
| POST /control/v1/sources/{id}/confirm-rotation | 检查新代确认与回放后撤销旧代 |
| POST /control/v1/sources/{id}/cancel-rotation | 撤销新代，保留旧代 |
| POST /control/v1/sources/{id}/disable；POST /control/v1/sources/{id}/enable | 停用、用新凭据恢复 |
| POST /control/v1/sources/{id}/rename；GET /control/v1/audit | 名称备注与审计 |
| POST /agent/v1/heartbeat；POST /agent/v1/config-acks | 来源 token 确认本站配置与状态 |

来源变更接口要求 `Idempotency-Key`。同一键和同一输入返回同一作业；不同输入复用同一键会被拒绝。浏览器把未决操作键暂存到 sessionStorage，不保存秘密，网络结果不明时重试不会重复创建来源。

## 容量与保留

本次两站各分配 **96 GiB/副本** 的 raw 缓冲，即每分区 16 GiB，新增来源总额度 192 GiB。现场已有其他接入使用 legacy raw.default，保留其 384 GiB 预算；clean 仍为 252 GiB，另有 DLQ/receipt 少量额度。总上限约 837 GiB/副本，给 1 TiB broker 卷保留约 18% 的余量；分段删除有滞后，应继续监测实际磁盘水位。

原设计假定旧 raw 退出后把 384 GiB 全部分给两站；现场前提已变化，因此本次没有削减其他接入方额度或撤销共享历史账号。新站点只用各自独立凭据。日后迁完 legacy 或扩盘再提高额度，禁止给每个新来源复制一整份预算。

72 小时和字节上限先到者生效，分区偏斜可能更早淘汰；96 GiB 不代表保证三天。当前每来源配额 2 MiB/s **每 broker**，不是全群总速率，不能据此承诺公网峰值。

日志正文经有限元数据清洗、包内重复块去重和 ZSTD 压缩后长期保存在 HDD R6 的自包含 Parquet 包，不做摘要替代，不引入 HDFS。ClickHouse 只存检索字段和目录：7 天后迁 HDD、180 天后清理事件明细索引，归档目录长期保留。

在两站原始总量 50–100 GB/天基础上，最终落盘若是原始量 10%，约 1.83–3.65 TB/年；若是 25%，约 4.56–9.13 TB/年。实际比例必须采样测量，并计入重试和双边界采集放大。HDD 正文仍缺独立第二副本；本次来源管理没有改变这一现状。

## 运行监控

`metrics.py` 由 root 所有的定时采集器执行，输出服务、消费积压、Topic/分区保留量、注册库、配置任务积压、最近观察时间和凭据年龄，不输出正文或秘密。`monitoring-rules.yml` 共 15 条规则，新增中央配置任务卡住、来源控制观察失败和凭据到期轮换提醒。凭据 90 天规则只是提醒，不自动吊销。

各来源心跳已导出为指标并在页面显示，但实际公网站点尚未部署，因此没有提前打开来源心跳离线告警。源端上线时再接入该告警以及采集丢失计数；否则中央服务正常不代表每台公网源端正常。

## 验收与后续范围

远程测试覆盖注册幂等、容量限制、秘密不进入公开状态、心跳来源绑定、浏览器 CSRF、轮换前置条件、停用/恢复、并发注册发布顺序、配置过期、注册库恢复、同 ID 跨来源归档及查询筛选。

真实 Kafka → 清洗 → ClickHouse → HDD 验收使用中央测试机的合成事件，检查跨 Topic 拒绝、伪造来源和 legacy 冒用进入 DLQ、两站相同请求 ID 不混淆、旧连接撤权和新连接认证失败。合成心跳主机为 `acceptance-vm624 (not production)`；安装器实机测试报告 `ai-query-01`，随后测试 shipper 已停用并移除配置。两者都不是公网生产实例。

尚未加入一次性接入码、宿主机远程执行、自动配置拉取、自动定期换密钥或 Sub2API 内嵌设置页。第一阶段的入口是中央管理页与宿主机配置包安装，不需要另建前端项目或改两套 Sub2API 代码。
