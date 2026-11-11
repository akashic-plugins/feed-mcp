# feed-mcp

Feed 是一个 Akashic Plugin API v3 插件。它用现有的几个正交能力组合出完整
Feed 链路：

```text
Core Timer ──触发──> Feed source ──emit──> 值班（watch.events.v1）
                         │
                         └──交付成功后在 Feed 里结算该 revision

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
  deadline；完成拉取、交付和 deadline 持久化后再注册下一次。
- `watch.events.v1`：存在时才启动来源。每个待处理条目对应一次
  `emit(source_name, title, text, url, at, key)`，`key` 为 `event_id:content_hash`，
  交付成功立刻在 Feed 里结算（`event_id` 记入 ACK 表）；交付失败则不结算，
  下一轮用同一个 `key` 重试，值班侧按 key 去重。

升级到直接交给值班的版本时，旧流程里停在"未结算"的条目不会重交：第一次启动
（`metadata.watch_emit_migrated` 不存在）会把当时所有未结算条目一次性标记为已处理。

Feed 的 SQLite 是 provider 事实 owner：订阅、完整 item、当前 poll state、ACK
和每个已导出 revision 的冻结 payload 都留在 `feed_mcp.sqlite3`。值班是事件
接收方。纯诊断日志固定为 5 MiB、最多 3 个备份；空轮询只更新当前
deadline，不交付事件，也不制造持久内容历史。

候选 Root 不会收到 `runtime.started`，因此不会注册 Timer、访问外部 Feed 或写正式数据库。

## 目录

```text
feed-mcp
├─ akashic.plugin.toml
├─ plugin.py                 # 组合工具、Timer、值班事件来源与脚本执行
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

## 验证

CI 固定 Core `d64545463faf5500ff651fa7337229c01b366393`，运行单元测试、真实
Manager + 工具 + Timer fixture 与记录 emit 的值班入口替身、pyright、compileall 和
`git diff --check`。Manager fixture 还证明：候选零 Timer/零正式写，发布时旧
Timer 已取消后新稳定 Root 才接班。
