# feed-mcp

Feed 是一个 Akashic Plugin API v3 插件。它用现有的三个正交能力组合出完整
Feed 链路：

```text
Core Timer ──触发──> Feed source ──submit──> Content inbox
                         │                         │
                         └──精确 revision ACK <───┘

用户 Turn ──调用──> Feed MCP ──只做──> 订阅管理 / 缓存查询
```

## 能力与 owner

- `MCP_SERVERS`：只注册用户主动调用的 `feed_manage`、`feed_query`。MCP
  lifespan 不启动后台轮询，也没有主动内容专用工具。
- `TIMERS`：正式稳定 Root 拥有唯一轮询 Timer。每次只注册一个 one-shot
  deadline；完成 settlement、拉取、提交和 deadline 持久化后再注册下一次。
- `content.source.v1`：以 `feed-subscriptions` 身份提交完整 Feed item，并接收
  Content 的 delivery settlement。ACK 精确绑定 `event_id + content_hash`；旧
  revision 的 ACK 不会误确认新内容。

Feed 的 SQLite 是 provider 事实 owner：订阅、完整 item、当前 poll state、ACK
和每个已导出 revision 的冻结 payload 都留在 `feed_mcp.sqlite3`。Content 是待处理
与投递状态 owner。纯诊断日志固定为 5 MiB、最多 3 个备份；空轮询只更新当前
deadline，不提交 Content，也不制造持久内容历史。

候选版本允许自己的 MCP managed-process 完成 readiness/handshake，但
`candidate_read_only_tools = []`，且 recording backend 不接触 Feed 数据。候选 Root
不会收到 `runtime.started`，因此不会注册 Timer、访问外部 Feed 或写正式数据库。

## 目录

```text
feed-mcp
├─ akashic.plugin.toml
├─ plugin.py                 # 组合 MCP、Timer、Content source
├─ content_source.py         # Feed source 私有生命周期
├─ feed_runtime/backend.py   # MCP 与 source 共享的唯一 Feed domain 实现
├─ skills/
└─ mcp/
   ├─ run_mcp.py
   └─ src/mcp_bridge.py      # 薄 MCP adapter
```

正式数据由 Core 分配到 `plugin-data/feed-<marketplace>/`。从 v2 迁移仍使用
`scripts/migrate_v2_data.py`；它保留源数据并产生可核对的 migration receipt。

## 验证

CI 固定 Core `9da3a988a2bf62b0f550bd4f6bb98c4eeb1f56f5`，运行单元测试、真实
Manager + stdio MCP + Content + Timer fixture、pyright、compileall 和
`git diff --check`。Manager fixture 还证明：候选零 Timer/零正式写，发布时旧
Timer 已取消后新稳定 Root 才接班。
