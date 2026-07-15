# feed-mcp

`feed-mcp` 是一个 Aka 插件试点仓库，打包了三类能力：

- `lifecycle`: 最小 `FeedPlugin`
- `skills`: `feed-manage` 与 `rsshub-route-finder`
- `mcp`: feed 订阅查询、缓存自刷新与主动事件读取

目录结构：

```text
feed-mcp
├─ plugin.py
├─ skills/
│  ├─ feed-manage/
│  └─ rsshub-route-finder/
└─ mcp/
   ├─ run_mcp.py
   └─ src/
```

本仓库用于验证：

- `plugin.py` 程序化声明生命周期、skills、MCP 与主动信息源
- `~/.akashic-plugin/cache` 下的 installed plugin 装载
- skill 软链接
- 插件程序化 MCP 注册

运行时目录：

```text
~/.akashic-plugin
├─ cache/
│  └─ <marketplace>/feed/<version>/
│     ├─ plugin.py
│     ├─ skills/
│     └─ mcp/
└─ data/
   └─ feed-<marketplace>/
      ├─ feed_mcp.sqlite3
      ├─ source_scores.json
      ├─ feed_cache.db
      ├─ feed_mcp.runtime.log
      ├─ feed_mcp.runtime.log.1
      ├─ feed_mcp.runtime.log.2
      └─ feed_mcp.runtime.log.3
```

边界约定：

- `cache` 只放代码包与依赖环境，可被新版本替换
- `data` 只放运行时状态与历史数据，升级时保留
- 仓库本身不提交 sqlite、日志、运行态缓存
- 运行日志按 5MB 轮转，最多保留 3 个历史文件

当前 feed 的持久化方式：

- 新增/取消订阅通过 `feed_manage` 直接读写 sqlite `sources`
- 历史内容保存在 sqlite `items`
- 主动推送确认状态保存在 sqlite `acked_items`
- 轮询状态保存在 sqlite `poll_state`

缓存 freshness：

- MCP 进程通过 FastMCP lifespan 启动唯一后台 poller
- 启动后立即刷新一次；首次主动事件读取会等待该刷新完成
- 后续按 `feed_mcp.json` 的 `poll_ttl_seconds` 定时刷新
- `get_proactive_events` 只读取稳定缓存，不承担网络抓取
- SQLite 使用 WAL 和 busy timeout，轮询写入不会阻塞缓存快照读取

首次迁移行为：

- 插件首次启动时，如果 `$AKA_PLUGIN_DATA_DIR/feed_mcp.sqlite3` 不存在
- 会尝试从旧目录复制历史数据
  - `$AKASHIC_WORKSPACE/mcp/feed-mcp/`
  - `$AKASHIC_WORKSPACE/backups/feed-plugin-migration-*/feed-mcp/`
- 迁移的是运行态数据，不是把数据打包进仓库
