# feed-mcp

`feed-mcp` 是一个 Aka 插件试点仓库，打包了三类能力：

- `lifecycle`: 最小 `FeedPlugin`
- `skills`: `feed-manage` 与 `rsshub-route-finder`
- `mcp`: feed 订阅查询与主动轮询工具

目录结构：

```text
feed-mcp
├─ .aka-plugin/plugin.json
├─ plugin.py
├─ skills/
│  ├─ feed-manage/
│  └─ rsshub-route-finder/
└─ mcp/
   ├─ servers.json
   ├─ run_mcp.py
   └─ src/
```

本仓库用于验证：

- `.aka-plugin/plugin.json` 声明模型
- `~/.akashic-plugin/cache` 下的 installed plugin 装载
- skill 软链接
- 插件声明式 MCP 注册

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
      └─ feed_mcp.runtime.log
```

边界约定：

- `cache` 只放代码包与依赖环境，可被新版本替换
- `data` 只放运行时状态与历史数据，升级时保留
- 仓库本身不提交 sqlite、日志、运行态缓存

当前 feed 的持久化方式：

- 新增/取消订阅通过 `feed_manage` 直接读写 sqlite `sources`
- 历史内容保存在 sqlite `items`
- 主动推送确认状态保存在 sqlite `acked_items`
- 轮询状态保存在 sqlite `poll_state`

首次迁移行为：

- 插件首次启动时，如果 `~/.akashic-plugin/data/feed-<marketplace>/feed_mcp.sqlite3` 不存在
- 会尝试从旧目录复制历史数据
  - `~/.akashic/workspace/mcp/feed-mcp/`
  - `~/.akashic/workspace/backups/feed-plugin-migration-*/feed-mcp/`
- 迁移的是运行态数据，不是把数据打包进仓库
