# feed-mcp

Feed 是一个 Akashic Plugin API v3 插件，提供 RSS 订阅管理、Feed MCP 和
`subscriptions` 主动内容源，同时保留 `skills/` 下的 Feed 技能。

```text
feed-mcp
├─ akashic.plugin.toml
├─ plugin.py
├─ scripts/migrate_v2_data.py
├─ skills/
└─ mcp/
   ├─ run_mcp.py
   └─ src/
```

`plugin.py` 只执行 `apply(ctx, config)` 声明，不启动进程、不访问网络、不读写
插件数据。Core 从静态 manifest 准备 MCP runtime，并按配置注册
`subscriptions` 主动事件源；`skill_roots = ("skills",)` 保持原有技能装载路径。

正式运行数据位于 Core 分配的 `plugin-data/feed-<marketplace>/`：

- `feed_mcp.sqlite3`：订阅、条目、确认和轮询状态
- `source_scores.json`、`feed_cache.db`：v2 历史运行数据（如存在）
- 运行日志只通过 MCP stderr 输出，不创建 `feed_mcp.runtime.log`

候选验证使用 `FEED_BACKEND=recording`：

- `get_proactive_events` 固定返回 `{"status":"empty"}`
- 不启动 `FeedPoller`，不访问 RSS/RSSHub、不连接 SQLite
- `acknowledge_events` 在 recording 后端 fail-loud
- candidate 只开放只读的 `get_proactive_events`

正式主动端口使用明确的 typed 结果：拉取返回 `empty` 或 `items`，确认只有全部
请求 ID 持久成功时才返回 `committed`；异常和部分确认返回 `failure`，不会伪装
为成功。

## 从 v2 迁移

先停止占用 workspace 的 Akashic runtime，再运行：

```bash
PYTHONPATH=/path/to/akashic-agent \
python scripts/migrate_v2_data.py \
  --workspace /path/to/workspace \
  --marketplace github
```

迁移脚本持有 workspace 独占锁，按 `mcp/feed-mcp/` primary、再按
`backups/feed-plugin-migration-*/feed-mcp/` 最新备份顺序选择第一个含数据的源，
并保留源目录。`feed_mcp.sqlite3` 和其他 SQLite 数据使用在线 backup 后执行
integrity check；目标存在不同内容时直接失败。

最终 receipt 写入
`plugin-data/feed-<marketplace>/.feed-v2-migration.json`，逐文件记录
`source_missing`、`target_only`、`verified` 或 `copied`、源路径、SHA-256、大小和
SQLite integrity。进程内发布失败会回滚本次新增文件；进程崩溃后重跑会清理残留
staging、核对同内容目标并完成发布。源数据保留作为 recovery source；receipt
不属于候选验证输入。

完整外网 RSS E2E 不属于本插件工作流。v3 workflow 固定 Core
`78e50d4dfb3f4348fff37d55d9c9bdd0e002164d` 与 contracts
`4dd69dd621e029e51e99aa428443fa3a4ec1f6cf`，执行插件单元测试、pyright、
`compileall` 和 `git diff --check`，并以空订阅库走真实 Manager、stdio MCP、
committed proactive source lease 与 terminate cleanup。
