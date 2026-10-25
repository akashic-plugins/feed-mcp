---
name: feed-manage
description: 管理和查询信息来源订阅（RSS 与脚本来源）。用户问"你有什么信息来源"、"你在监控什么"、"有哪些动态"，要添加/删除订阅，或想关注一个没有 RSS 的网站、X 账号、论坛、榜单时使用。
---

# Feed 管理与查询

Feed 把每个来源的内容统一成**事件**，去重、排序、投递都由 Feed 负责。来源只有两种：

- **RSS**：有现成 feed 地址，用 `feed_manage(action="subscribe")`。
- **脚本**：没有 RSS 的页面，写一段宿主 shell 脚本抓取，用 `feed_script` 试跑、保存。

无论哪种，最后都必须收敛成下面这一种事件格式。

## 事件格式（脚本来源的唯一出口）

脚本**最后一行**打印一个 JSON 对象，之前的输出都只当日志：

```json
{"status": "ok", "events": [
  {"id": "xstatus:2113626655902126442",
   "url": "https://x.com/AnthropicAI/status/2113626655902126442",
   "title": "一行标题，可省略",
   "content": "正文，可省略，但 title 与 content 至少有一个",
   "author": "AnthropicAI",
   "published_at": "2026-10-23T13:40:38Z"}
], "note": "可选，给人看的说明"}
```

- `id`：同一来源内**稳定且唯一**。用页面上的原生 id（帖子号、文章 slug），不要用序号、抓取时间或会变的标题。
- `url`：http(s) 的条目原文链接，不是列表页地址。
- `published_at`：ISO-8601，带时区；页面没有时间就省略，不要拿抓取时间冒充。
- 只允许这六个字段；多余字段、类型错误、id 重复都会让整次结果判为 `invalid`。
- 一次最多 100 条，只输出页面上当前能看到的条目。

`status` 只能是：

| status | 什么时候用 |
|---|---|
| `ok` | 抓到至少一条事件 |
| `empty` | 页面正常，但确实没有条目 |
| `blocked` | 被反爬/人机验证/Cloudflare 拦住 |
| `login_required` | 需要登录或登录已过期 |
| `stale` | 拿到的是旧页面（没有真正重新加载） |
| `error` | 其他可识别的失败 |

`ok`/`empty` 之外的状态都不会改动已入库的条目，只记进来源健康，`feed_manage list` 能看到。**抓不到时如实报告状态，不要输出空的 ok。**

## 脚本只做一件事

- **无状态**：不判断新旧、不记上次抓到哪、不写文件。每次都输出页面上的全部当前条目，新旧由 Feed 按 id 判断。
- **自包含**：所有参数（账号、地址、会话名）直接写在脚本里；运行环境不会传入任何变量。
- **有界**：120 秒内结束，不要后台进程；输出超长会被截断判为 invalid，所以中间日志尽量少。
- 在宿主用登录 shell 执行；可用 `curl`、`jq`、`python3`、`node`、`opencli`。

## 接入一个新来源的流程

1. **先找 RSS**：站点自带 feed（`/feed`、`/rss`、`<link rel="alternate">`）或 RSSHub 路由（见 rsshub-route-finder skill）。有就直接 subscribe，不写脚本。
2. **再找接口**：页面背后有 JSON 接口时，用 `curl … | jq` 转成事件格式，比浏览器稳定得多。
3. **最后才用浏览器**：需要登录态或 JS 渲染时，用已登录的浏览器：
   ```sh
   opencli browser feed open "https://x.com/AnthropicAI?_fb=$(date +%s)" --window background >/dev/null
   sleep 5
   opencli browser feed eval '(async () => { …; return JSON.stringify({status: "ok", events}) })()' | grep '^{' | tail -n 1
   ```
   在 eval 里用 `Date.now() - performance.timeOrigin > 60000` 判断 `stale`，用登录按钮或跳转判断 `login_required`。
4. `feed_script(action="test", code=…)` 试跑，看报告里的 id、时间、标题是否合理；不对就改，直到 `status=ok`。
5. `feed_script(action="save", name=…, url=…, code=…, note="这一版做了什么")`。保存会**再跑一次**，只有 `status=ok` 才保存为新版本并立即开始定时抓取（默认间隔同 RSS，可用 `interval_seconds` 调大，最少 300 秒）。

## 维护

- `feed_manage(action="list")`：所有来源及健康（最近状态、条数、上次成功时间、最近错误）。
- 某个脚本来源连续报 `invalid`/`error`/`stale`：`feed_script(action="show", name=…)` 看当前代码，修好后 test → save 出新版本。
- `feed_script(action="history", name=…)` 看版本；新版本不如旧版时 `feed_script(action="use", name=…, version=N)` 回滚。
- `login_required`/`blocked` 不是脚本问题：告诉用户需要在浏览器里重新登录或该站点挡住了自动访问，不要反复改脚本。

## 查询与订阅管理

```
feed_manage(action="list")
feed_manage(action="subscribe", name="名称", url="RSS地址")
feed_manage(action="unsubscribe", name="名称")      # 删除历史内容
feed_manage(action="pause" | "resume", name="完整名称")   # 保留历史
feed_manage(action="update", name="完整名称", url="新 RSS 地址")
feed_query(action="latest", limit=10, source="名称")
feed_query(action="summary")
feed_query(action="search", keyword="关键词")
```

- 问"你有什么信息来源"时，先调 `feed_manage(action="list")` 拿完整列表再回答，不得凭记忆列举；之前调过也要重新调用。
