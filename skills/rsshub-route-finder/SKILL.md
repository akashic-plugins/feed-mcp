---
name: rsshub-route-finder
description: 输入任意网页 URL，查询本地 RSSHub 路由规则或生成 Twitter/X (xcancel) 专属 RSS 链接。触发词：rss, 订阅, feed, rsshub, 路由, 有没有 rss, 怎么订阅，生成 rss, twitter rss, x 账号订阅
metadata: {"akasic": {"requires": {"bins": ["uv", "curl"]}}}
---

# RSS 链接生成器 (RSSHub + Twitter/X 专属)

> **核心逻辑**：
> 1. **优先判断是否为 Twitter/X 链接**。如果是，走 **双阶段验证流程**：
>    - **阶段一**：优先尝试 `nitter.net`（轻量、无需白名单）。
>    - **阶段二**：若 nitter 失败，降级尝试 `rss.xcancel.com`（需复杂白名单验证）。
> 2. **非 Twitter/X 链接**，走本地 RSSHub 路由查找流程。

---

## 🐦 Twitter/X 专属处理流程（双阶段验证）

### 1. 触发条件
用户提到以下任一关键词：
- "Twitter RSS", "X 账号订阅", "twitter 转 rss"
- "生成 @xxx 的订阅链接", "验证可用性"
- 输入 URL 包含 `twitter.com` 或 `x.com`

### 2. 收集输入与规范化
从用户消息中提取账号名或 URL：
- 支持格式：`@NiKoCS_`, `https://x.com/NiKoCS_`, `https://twitter.com/NiKoCS_`
- **提取逻辑**：去掉 `@`, `https://`, `www.`, `twitter.com/`, `x.com/`，统一得到 `username`。
- **清洗**：去掉前后空格，保留下划线 `_`。
- 示例：`@NiKoCS_` → `NiKoCS_`

---

### 🟢 阶段一：优先尝试 Nitter (nitter.net)

#### 3.1 生成 Nitter 订阅地址
构造 URL：
- **主订阅地址**: `https://nitter.net/<username>/rss`
- **备用预览地址**: `https://nitter.net/<username>`

#### 3.2 连通性验证 (轻量级)
使用 `curl` 发起 GET 请求，检查 HTTP 状态码及内容：
```bash
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -A "Mozilla/5.0" \
  "https://nitter.net/<username>/rss")

BODY=$(curl -s -A "Mozilla/5.0" \
  "https://nitter.net/<username>/rss")

if [ "$HTTP_CODE" -eq 200 ] && echo "$BODY" | grep -q "<item>"; then
  echo "✅ Nitter 可用"
  # 直接返回 Nitter 链接，结束流程
elif [ "$HTTP_CODE" -eq 404 ]; then
  echo "❌ 账号不存在 (Nitter)"
  # 账号错误，无需尝试备选，直接报错
else
  echo "⚠️ Nitter 不可用 (HTTP $HTTP_CODE 或无内容)，尝试备选方案..."
  # 进入阶段二
fi
```
**判断标准**：
- ✅ **成功**: HTTP 200 且响应体含 `<item>`。**流程结束，返回 Nitter 链接。**
- ❌ **账号错误**: HTTP 404。直接提示用户检查账号，**不进入阶段二**。
- ⚠️ **服务不可用**: 其他状态码或无内容。**自动进入阶段二**。

---

### 🟠 阶段二：备选方案 RSS.xCancel (rss.xcancel.com)
*(仅当阶段一失败且账号存在时触发)*

#### 4.1 生成 xCancel 订阅地址
构造 URL：
- **备选订阅地址**: `https://rss.xcancel.com/<username>/rss`
- **备选预览地址**: `https://xcancel.com/<username>/rss`

#### 4.2 可用性验证 (严格模式)
**注意**：xcancel 通过 **TLS 指纹** 识别客户端，必须用系统 `curl`，且 UA 必须伪装成白名单客户端。
```bash
BODY=$(curl -s -A "FreshRSS/1.24.0" \
  -H "Accept: */*" \
  "https://rss.xcancel.com/<username>/rss")

if echo "$BODY" | grep -qi "not yet whitelisted"; then
  echo "❌ 被 xcancel 白名单拦截"
elif echo "$BODY" | grep -q "<item>"; then
  echo "✅ xCancel 可用"
else
  echo "⚠️ 返回内容异常（账号不存在或无推文）"
fi
```
**判断标准**：
- ✅ **可用**: 响应体含 `<item>` 标签。
- ❌ **白名单拦截**: 响应体含 `not yet whitelisted`（即使 HTTP 200 也视为不可用）。
- ⚠️ **账号异常**: 无 `<item>` 且无 whitelist 提示。

**重要参数**：
- **UA**: 必须使用 `FreshRSS/1.24.0`（已验证在白名单内）。
- **Header**: `Accept: */*`。

#### 4.3 异常处理
- **白名单拦截**: 提示 "rss.xcancel.com 拦截了此请求（TLS 指纹限制）。建议：1. 继续使用 Nitter 实例；2. 若必须用此源，需手动申请白名单或使用系统后台 curl 巡检。"
- **超时/无响应**: 提示 "rss.xcancel.com 服务暂不可用"。

---

### 5. 最终输出结果
根据验证结果返回结构化信息：

**情况 A：Nitter 成功**
> ✅ **Twitter/X RSS 生成成功 (Nitter)**
> - **账号**: @<username>
> - **订阅链接**: `https://nitter.net/<username>/rss`
> - **状态**: 可用 (HTTP 200, 含推文)
> - **建议**: 首选方案，轻量稳定。

**情况 B：Nitter 失败，xCancel 成功**
> ⚠️ **Twitter/X RSS 生成成功 (xCancel 备选)**
> - **账号**: @<username>
> - **订阅链接**: `https://rss.xcancel.com/<username>/rss`
> - **状态**: 可用 (Nitter 不可用，已降级至 xCancel)
> - **注意**: 此源可能被 TLS 指纹拦截，建议由系统后台通过 curl 定期巡检。

**情况 C：双重失败**
> ❌ **无法生成有效 RSS**
> - **Nitter**: 不可用 (HTTP xxx)
> - **xCancel**: 被白名单拦截 / 无内容
> - **建议**: 该账号可能已被保护、注销，或所有公共 RSS 网关均不可用。

---

## 🌐 非 Twitter/X 链接处理流程 (RSSHub)

### 执行
调用本地脚本查找 RSSHub 路由：
```bash
SKILL_DIR="$PWD/skills/rsshub-route-finder"
bash "$SKILL_DIR/scripts/find_route.sh" "<用户输入的 URL>"
```

### 常见错误
| 错误 | 解决 |
|------|------|
| 无法连接 RSSHub | `docker run -d --name rsshub -p 1200:1200 diygod/rsshub` |
| 路由未找到 | 告知用户该网站可能不被支持，建议访问 `http://localhost:1200` 手动查找 |

### 输出处理
- 展示所有匹配的订阅链接。
- 多个结果时，简要说明各链接区别（如：回答更新 vs 文章更新）。
- 若链接中仍含 `:param` 占位符，提示用户补充对应参数。

---

## 📝 示例对话

**用户**: "帮我把 @NiKoCS_ 转成 RSS"
**Bot**:
1. 识别为 Twitter 账号 → 提取 username: `NiKoCS_`
2. 生成 URL: `https://rss.xcancel.com/NiKoCS_/rss`
3. 执行 `curl` 验证...
4. 返回:
   > ✅ **Twitter/X RSS 生成成功**
   > - **账号**: @NiKoCS_
   > - **订阅链接**: `https://rss.xcancel.com/NiKoCS_/rss`
   > - **状态**: 可用 (HTTP 200)
   > - **最近更新**: 2 小时前
   > - **建议**: 在 RSS 阅读器中设置 30-60 分钟刷新一次。

**用户**: "https://space.bilibili.com/12345 怎么订阅？"
**Bot**:
1. 识别为非 Twitter 链接 → 调用 RSSHub 脚本。
2. 返回匹配的 RSSHub 路由。

---

## ⚠️ 注意事项
- **必须执行 `curl` 验证**，不得直接返回未经验证的链接。
- **不能用 HTTP 状态码判断 xcancel 是否可用**：被拦截时仍返回 HTTP 200，必须检查响应体是否含 `not yet whitelisted`。
- **UA 固定用 `FreshRSS/1.24.0`**：xcancel 通过 TLS 指纹 + UA 双重验证，此 UA 已确认在白名单内。
- **隐私提示**：提醒用户 `rss.xcancel.com` 是第三方服务，不要用于私密账号。
