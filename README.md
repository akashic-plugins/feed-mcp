# feed-mcp

Feed 是一个 Akashic Plugin API v3 插件。它用现有的三个正交能力组合出完整
Feed 链路：

```text
Core Timer ──触发──> Feed source ──submit──> Content inbox
                         │                         │
                         └──精确 revision ACK <───┘

用户 Turn ──调用──> feed_manage / feed_query / feed_script
                         │
宿主进程 <──执行──── 脚本来源（需 host.processes.v1）
```

## 能力与 owner

- `TOOLS`：原生工具 `feed_manage`（订阅与健康）、`feed_query`（缓存查询），
  直接读写本插件数据目录，不启动子进程，也不触发抓取。
- `host.processes.v1`（可选）：存在时子 Fiber 提供脚本执行器并注册 `feed_script`；
  脚本来源在宿主执行，最后一行输出 `feed_runtime/events.py` 定义的事件 JSON。
  缺席时 RSS 照常，脚本来源记为 `unavailable`。格式与接入流程见
  `skills/feed-manage/SKILL.md`。
- `TIMERS`：正式稳定 Root 拥有唯一轮询 Timer。每次只注册一个 one-shot
  deadline；完成 settlement、拉取、提交和 deadline 持久化后再注册下一次。
- `content.source.v1`：以 `feed-subscriptions` 身份提交完整 Feed item，并接收
  Content 的 delivery settlement。ACK 精确绑定 `event_id + content_hash`；旧
  revision 的 ACK 不会误确认新内容。

Feed 的 SQLite 是 provider 事实 owner：订阅、完整 item、当前 poll state、ACK
和每个已导出 revision 的冻结 payload 都留在 `feed_mcp.sqlite3`。Content 是待处理
与投递状态 owner。纯诊断日志固定为 5 MiB、最多 3 个备份；空轮询只更新当前
deadline，不提交 Content，也不制造持久内容历史。

候选 Root 不会收到 `runtime.started`，因此不会注册 Timer、访问外部 Feed 或写正式数据库。

## 目录

```text
feed-mcp
├─ akashic.plugin.toml
├─ plugin.py                 # 组合工具、Timer、Content source 与脚本执行
├─ tools.py                  # feed_manage / feed_query
├─ script_tool.py            # feed_script：试跑、保存、版本与回滚
├─ script_runner.py          # 宿主执行脚本并按契约解析输出
├─ content_source.py         # Feed source 私有生命周期
├─ feed_runtime/backend.py   # 工具与 source 共享的唯一 Feed domain 实现
├─ feed_runtime/events.py    # 脚本来源的事件契约
├─ feed_runtime/scripts.py   # 脚本来源的存储、到期与入库
├─ skills/
└─ mcp/                      # 插件 Python 依赖、配置与离线脚本（历史目录名）
```

正式数据由 Core 分配到 `plugin-data/feed-<marketplace>/`。从 v2 迁移仍使用
`scripts/migrate_v2_data.py`；它保留源数据并产生可核对的 migration receipt。

旧 proactive island 中归 Feed 所有的 Wake facts 由
`scripts/retire_legacy_feed_backlog.py` 负责切换。默认 `--plan` 只读输出 Feed
source inventory 与当前 provider backlog 的 count/digest；显式 `--apply` 必须回传
这些 exact 值，并把整批 pre-cutover provider revision 原子标记为
`cutover_superseded`。该路径不向 Content 提交旧条目，重复执行只接受同一 batch receipt；
它不会读取或改变其他插件的 workspace 状态。

## 验证

CI 固定 Core `065dfb7fb37534c57ed500b1ed9b5deb7090cdb0`，运行单元测试、真实
Manager + 工具 + Content + Timer fixture、pyright、compileall 和
`git diff --check`。Manager fixture 还证明：候选零 Timer/零正式写，发布时旧
Timer 已取消后新稳定 Root 才接班。
