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
