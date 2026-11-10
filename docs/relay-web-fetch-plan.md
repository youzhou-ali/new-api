# 中转站网页抓取方案（new-api `POST /v1/fetch`）

日期：2026-11-10。状态：new-api 已在 `feat/web-fetch` 实施，待发布。

需求来源：《中转站网页抓取需求》（2026-11-08 草稿，<https://claude.ai/artifact/MAvuL95rBnYA672GVwsamy>）。本文是实现依据，与草稿不一致的地方以本文为准，差异见第 12 节。

基线：new-api `main` 提交 `cd556887e`（仓库在 `~/code/newapi`）。除特别说明，文中路径都相对 new-api 仓库根目录。本文只覆盖中转站，Cube 的回退调用另做（第 14 节）。

## 1 开工前

- 完整阅读 `AGENTS.md`、`.agents/rules/billing.md`（本次改 `relay/request_billing.go`、`relay/helper/valid_request.go`、`service/text_quota.go`，属于计费改动）、`web/AGENTS.md`，前端还要读其中要求的 `shadcn-ui` 技能。
- 从 `main` 建分支 `feat/web-fetch`。
- 根模块需要 Go 1.26。改完 `relaykit/` 后在该目录运行 `GOWORK=off go build ./...`。
- `AGENTS.md` 禁止未经明确要求在 `docs/` 下新增文件，所以不写 ADR，只改已有的 `docs/openapi/relay.json`。
- 不改表结构，渠道类型只是一个新取值，不需要三库迁移验证。

## 2 要解决的问题

部署在国内的 Cube Worker 访问 GitHub 等海外站点时常常连不上。中转站主机能稳定直连 GitHub（2026-11-09 确认），所以让中转站代发一次 HTTP(S) GET，把目标网站的原始响应交回 Cube，正文提取仍在 Cube。

抓取在网关进程内执行，没有外部上游。渠道在这里不代表上游，用它是为了复用分组权限、后台开关、定价和消费日志，这些都绑定在渠道和模型上。

## 3 关键决定

| 决定 | 原因（代码位置按基线） |
|---|---|
| 不复用 `service.GetSSRFProtectedHTTPClient`，新建独立 Transport | 现有客户端受管理员 `fetch_setting` 控制：关闭防护后退回普通客户端（`service/http_client.go:160`），默认放行 8080/8443（`setting/system_setting/fetch_setting.go:23`）；用环境代理时跳过拨号校验（`service/protected_fetch_client.go:146`）；跟随全局 `TLSInsecureSkipVerify`；重定向上限 10 次；错误是拼接字符串，无法分类 |
| 地址判断复用 `common.SSRFProtection.IsIPAccessAllowed`，配置用零值 `&common.SSRFProtection{}` | 零值是黑名单模式加空名单，只拦私有和保留地址。不能用 `DefaultSSRFProtection`，它是白名单模式，会拒绝所有地址 |
| 用 `net.Dialer.ControlContext` 校验实际连接的 IP | 能防 DNS 重绑定，也覆盖 `127.1`、十进制 IP 这类字符串校验漏掉的写法 |
| 目标站的所有失败都带 `types.ErrOptionWithSkipRetry()` | 这是目标网站的问题，不是渠道的问题。否则 502 会按默认规则重试（`setting/operation_setting/status_code_ranges.go:26`），同一出口重试没有意义，还会叠加 30 秒。`ShouldDisableChannel` 遇到该选项也会提前返回 false |
| 价格用内置表达式 `tier("request", fixed(0))` | 模型固定价 0 走不到 `FixedPriceBilling`，`hasBillableUsage`（`service/text_quota.go:76`）判 false，每次调用写一条 ERROR 日志，消费日志里还会出现“上游未返回用量”。`.agents/rules/billing.md` 也要求新内置价格写成表达式 |
| URL 内容校验放在抓取循环里 | 请求校验层的错误码写死为 `invalid_request`（`controller/relay.go:131`），放在那里得不到 `invalid_url`。首跳和重定向也因此共用一套校验 |
| `/fetch` 注册在 `ModelRequestRateLimit` 之前 | 代抓不应占用用户的模型调用限流，它有自己的按 Key 限流 |
| 拒绝给 WebFetch 渠道配 Proxy | 走代理时连接由代理发起，拨号校验失效 |
| 错误信息只用固定文案 | Go 的 `*url.Error` 文本带完整 URL 和查询串，需求要求不记查询串 |
| 命名统一用 `WebFetch` | 仓库已有 `RelayModeToolsFetch`、`DeepSeekFetchModel`（DeepSeek 的上游接口），泛称 `Fetch` 容易混淆 |

## 4 接口

### 4.1 请求

```http
POST /v1/fetch
Authorization: Bearer sk-...
Content-Type: application/json
X-Usage-Tag: ...
X-Usage-Ref: ...

{"model":"web-fetch","url":"https://github.com/mindshake-ai/cube"}
```

- `Content-Type` 必须是 `application/json`。
- 字段白名单只有 `model`、`url`，其他字段返回 400，写法同 `relay/helper/search_request.go`。
- `model` 必须等于 `web-fetch`。
- `url` 必须是非空字符串，内容校验在抓取时做。
- `X-Usage-*` 已由全局 `middleware.UsageTags()` 处理，不用改。

### 4.2 成功（HTTP 200）

目标网站只要给出响应，无论状态码是多少都算成功。

```json
{"url":"https://github.com/mindshake-ai/cube","status":200,"content_type":"text/html; charset=utf-8","body":"PGh0bWw+..."}
```

| 字段 | 含义 |
|---|---|
| `url` | 重定向后的最终地址 |
| `status` | 目标状态码，原样返回 |
| `content_type` | 目标 `Content-Type` 原文，没有时为空字符串 |
| `body` | 解压后的正文，base64 编码，没有正文时为空字符串 |

```go
// relaykit/dto/web_fetch.go
type WebFetchRequest struct {
	Model string `json:"model"`
	URL   string `json:"url"`
}

// GetTokenCountMeta 不带 CombineText，URL 不进入 token 估算和敏感词检查。
func (r *WebFetchRequest) GetTokenCountMeta() *types.TokenCountMeta {
	return &types.TokenCountMeta{TokenType: types.TokenTypeTokenizer}
}
func (r *WebFetchRequest) IsStream(_ *http.Request) bool { return false }
func (r *WebFetchRequest) SetModelName(model string)     { r.Model = model }

type WebFetchResponse struct {
	URL         string `json:"url"`
	Status      int    `json:"status"`
	ContentType string `json:"content_type"`
	Body        []byte `json:"body"` // JSON 序列化时自动 base64；必须是非 nil，否则输出 null
}
```

`io.ReadAll` 对空正文返回非 nil 的空切片，满足“空字符串”的要求。

### 4.3 失败

格式沿用 `{"error":{"message","type","code"}}`。`type` 为 `new_api_error`，框架会在 `message` 末尾追加 `(request id: ...)`。

目标站相关的错误（全部带 `ErrOptionWithSkipRetry`）：

| 情况 | HTTP | `error.code` | i18n key | zh-CN | en |
|---|---|---|---|---|---|
| URL 不合法 | 400 | `invalid_url` | `web_fetch.invalid_url` | 网址无效：只支持 80 或 443 端口的 http、https 地址，不能带用户名或密码，长度不超过 4000 | Invalid URL: only http or https on port 80 or 443 is allowed, without credentials, up to 4000 characters |
| 不是公网地址（含任何一跳） | 400 | `blocked_address` | `web_fetch.blocked_address` | 目标地址不是公网地址，已拒绝访问 | The target address is not public and was refused |
| 域名解析失败 | 502 | `dns_failed` | `web_fetch.dns_failed` | 无法解析目标域名 | Could not resolve the target host |
| 连接、TLS 失败或连接被重置 | 502 | `connect_failed` | `web_fetch.connect_failed` | 无法连接目标网站 | Could not connect to the target site |
| 重定向超过 5 次 | 502 | `too_many_redirects` | `web_fetch.too_many_redirects` | 目标网站重定向超过 5 次 | The target redirected more than 5 times |
| 正文超过 10 MB | 502 | `too_large` | `web_fetch.too_large` | 目标内容超过 10 MB | The target content exceeds 10 MB |
| 返回了不支持的内容编码 | 502 | `unsupported_encoding` | `web_fetch.unsupported_encoding` | 目标网站返回了不支持的内容编码 | The target returned an unsupported content encoding |
| 30 秒内未完成 | 504 | `timeout` | `web_fetch.timeout` | 目标网站 30 秒内未完成响应 | The target did not finish within 30 seconds |

其他错误：

| 情况 | HTTP | `error.code` | i18n key | zh-CN | en |
|---|---|---|---|---|---|
| 请求不是 JSON | 400 | `invalid_request` | `web_fetch.json_required` | 抓取请求需要 application/json | Web fetch requires application/json |
| 字段不在白名单 | 400 | `invalid_request` | `web_fetch.unsupported_field` | 不支持的抓取字段：{{.Field}} | Unsupported web fetch field: {{.Field}} |
| `model` 不对（在 `Distribute` 拦截，文案由 `MsgDistributorInvalidRequest` 包一层，同 `/v1/search`） | 400 | 空 | `web_fetch.invalid_model` | model 必须为 web-fetch | model must be web-fetch |
| `url` 为空 | 400 | `invalid_request` | `web_fetch.url_required` | url 不能为空 | url is required |
| 渠道类型不匹配 | 400 | `invalid_request` | `web_fetch.invalid_channel` | 抓取模型与所选渠道不匹配 | The web fetch model does not match the selected channel |
| 渠道有映射、覆盖或代理 | 400 | `invalid_request` | `web_fetch.channel_overrides` | 抓取渠道不能映射模型、覆盖参数或配置代理 | Web fetch channels cannot map models, override parameters or use a proxy |
| 价格被改成非按次 | 400 | `model_price_error` | `web_fetch.fixed_price_required` | web-fetch 需要按请求固定价格 | web-fetch requires a fixed per-request price |
| 单 Key 超限 | 429 | `rate_limit_exceeded` | `web_fetch.rate_limited` | 抓取请求过于频繁，请稍后重试 | Too many web fetch requests, please retry later |
| 全局并发已满 | 429 | `rate_limit_exceeded` | `web_fetch.busy` | 抓取服务繁忙，请稍后重试 | Web fetch is busy, please retry later |

Key 无效、无模型权限、额度不足沿用现有行为。zh-TW 按 zh-CN 翻译。

## 5 抓取器 `service/web_fetch.go`

规则：

1. 只发 GET，不带请求体。
2. 请求头只设 `User-Agent: Mozilla/5.0 (compatible; CubeAgent/relay)` 和 `Accept`。不要设 `Accept-Encoding`，Go 会自动发 `gzip` 并透明解压，10 MB 限制因此按解压后计算。
3. 不用 cookie jar。
4. 手动跟随 301、302、303、307、308（带 `Location` 时），最多 5 次。`http.Client` 自动跟随会添加 `Referer`，违反规则 2。其他 3xx 和没有 `Location` 的 3xx 按目标响应原样返回。
5. 每一跳重新校验 URL：协议只能是 http、https，不能带用户信息，不能是 opaque，必须有主机名，长度不超过 4000，端口只能为空、80 或 443。
6. 不读代理环境变量，不读 `TLSInsecureSkipVerify`，不读 `fetch_setting`，不应用渠道的请求头覆盖。
7. 整次抓取受 30 秒 context 约束，超时在 `PerformWebFetch` 里设置，便于测试传入更短的 context。
8. 响应解压后仍带非 `identity` 的 `Content-Encoding` 且正文非空时，返回 `unsupported_encoding`。

参考实现（命名和结构可调整，语义不能变）：

```go
const (
	WebFetchTimeout      = 30 * time.Second
	webFetchMaxBodyBytes = 10 << 20
	webFetchMaxRedirects = 5
	webFetchMaxURLLength = 4000
	webFetchUserAgent    = "Mozilla/5.0 (compatible; CubeAgent/relay)"
	webFetchAccept       = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
)

var errWebFetchBlocked = errors.New("web fetch target is not a public address")

type webFetcher struct {
	client    *http.Client
	allowPort func(port string) bool
}

type WebFetchResult struct {
	URL, ContentType, Host, Path string
	Status                       int
	Body                         []byte
}

type WebFetchError struct {
	Code       types.ErrorCode
	StatusCode int
	MessageKey string
	Host, Path string // 最后一次尝试的目标；URL 不合法时为空
	Detail     string // 只写服务端日志，取 url.Error.Err，不含 URL
}

func newWebFetcher(allowIP func(net.IP) bool, allowPort func(string) bool) *webFetcher {
	dialer := &net.Dialer{ControlContext: func(_ context.Context, _, address string, _ syscall.RawConn) error {
		host, _, err := net.SplitHostPort(address)
		if err != nil || !allowIP(net.ParseIP(host)) { // 带 zone 的地址解析为 nil，按拒绝处理
			return errWebFetchBlocked
		}
		return nil
	}}
	return &webFetcher{allowPort: allowPort, client: &http.Client{
		Transport: &http.Transport{
			DialContext:            dialer.DialContext,
			ForceAttemptHTTP2:      true, // 自定义 DialContext 时必须显式开启
			DisableKeepAlives:      true, // 目标任意，连接复用没有价值
			MaxResponseHeaderBytes: 1 << 20,
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// 循环 webFetchMaxRedirects+1 次：校验 URL → 发请求 → 是重定向就解析 Location 进入下一轮 →
// 否则读正文（io.LimitReader 到上限 +1）并返回。循环走完返回 too_many_redirects。

// 错误分类的顺序不能变：
//   errors.Is(ctx.Err(), context.DeadlineExceeded) → timeout
//   errors.Is(err, errWebFetchBlocked)             → blocked_address
//   errors.As(err, *net.DNSError)                  → dns_failed
//   其余（含 TLS、重置、调用方断开）                → connect_failed
```

生产实例用 `newWebFetcher((&common.SSRFProtection{}).IsIPAccessAllowed, 只放行空、"80"、"443")`，保存在包级变量 `defaultWebFetcher`，对外函数 `WebFetch(ctx, rawURL)`。

测试接缝只加一个导出函数，放宽回环地址和端口，其他地址仍走固定策略：

```go
// AllowWebFetchLoopbackForTest lets tests reach httptest servers: loopback
// addresses and any port. Every other address still follows the fixed policy.
func AllowWebFetchLoopbackForTest() (restore func())
```

`common/ssrf_protection.go` 的 `privateIPv6Nets` 补上 `2002::/16`（6to4）、`::/96`（IPv4 兼容）、`fec0::/10`（已废弃的站点本地）。这是收紧，不影响现有调用。

## 6 处理器 `relay/web_fetch_handler.go`

结构对齐 `relay/search_handler.go`。`PerformWebFetch` 由中转和后台渠道测试共用，不记账、不写响应。

```go
func WebFetchHelper(c *gin.Context, info *relaycommon.RelayInfo) *types.NewAPIError {
	response, apiErr := PerformWebFetch(c, info)
	if apiErr != nil {
		return apiErr
	}
	service.PostTextConsumeQuota(c, info, &dto.Usage{}, nil)
	c.JSON(http.StatusOK, response)
	return nil
}
```

`PerformWebFetch` 的步骤：

1. `info.InitChannelMeta(c)`。
2. 校验 `request.Model == info.OriginModelName` 且 `info.ChannelType == constant.ChannelTypeWebFetch`，否则返回 `web_fetch.invalid_channel`。
3. 调 `helper.ModelMappedHelper(c, info, nil)`，有映射、`ParamOverride`、`PassThroughBodyEnabled` 或 `Proxy` 时返回 `web_fetch.channel_overrides`。这两类错误带 `ErrOptionWithSkipRetry`。
4. `context.WithTimeout(c.Request.Context(), service.WebFetchTimeout)` 后调 `service.WebFetch`。
5. 失败时写一行应用日志：`logger.LogWarn(c, common.LogText("web fetch failed: code=%s host=%s path=%s ms=%d detail=%s", ...))`，然后返回 `types.NewErrorWithStatusCode(errors.New(i18n.T(c, e.MessageKey)), e.Code, e.StatusCode, types.ErrOptionWithSkipRetry())`。
6. 成功时调 `info.SetFirstResponseTime()`，设置 `info.WebFetchLog`（第 8 节），返回 `dto.WebFetchResponse`。

## 7 限流 `middleware/web_fetch_limit.go`

| 限制 | 取值 | 实现 |
|---|---|---|
| 单 Key 每分钟 | 30 | 窗口键 `WF:token:<token_id>`。`common.RedisEnabled` 时用 `redisFixedWindowTake`，否则用 `inMemoryRateLimiter.Request`（先 `Init(common.RateLimitKeyExpirationDuration)`，可重复调用） |
| 单 Key 并发 | 5 | 进程内计数：互斥锁保护 `map[int]int`，计数归零时删除该键 |
| 全局并发 | 20 | 同一把锁保护一个总数 |

- 顺序：先占并发，再查窗口，这样并发超限的请求不消耗窗口。`defer` 释放并发。
- `token_id` 取 `c.GetInt("token_id")`，由 `TokenAuth` 设置。
- 超限用 `abortWithOpenAiMessage(c, http.StatusTooManyRequests, i18n.T(...), types.ErrorCode("rate_limit_exceeded"))`。不用 `writeRateLimited`，它没有响应体，Cube 拿不到原因。
- Redis 出错时按 `ModelRequestRateLimit` 的现有做法返回 500 `rate_limit_check_failed`。
- 并发用进程内计数，因为线上是单实例（`ops/deploy/RUNBOOK.md`）。
- 全局 20 的依据：单请求最坏约 40 MB（10 MB 正文加 base64 序列化缓冲），占满约 800 MB。发布前对照主机内存确认。

路由插在 `relayV1Router.Use(middleware.ModelRequestRateLimit())` 这一行之前：

```go
relayV1Router.POST("/fetch", middleware.WebFetchLimit(), middleware.Distribute(), func(c *gin.Context) {
	controller.Relay(c, types.RelayFormatWebFetch)
})
```

## 8 计费和日志

- 在 `setting/billing_setting/builtin_billing.go` 的 `builtinBillingExpr` 加 `constant.WebFetchModel: \`tier("request", fixed(0))\``，部署后不用配价。写法同紧邻的 `DeepSeekSearchModel`。`pkg/billingexpr/expr.md` 明确 `fixed(0)` 合法且免费。
- 管理员如果给 `web-fetch` 配了旧式固定价，`GetBillingMode` 会走比例模式，`PrepareRequestBilling` 的按次检查会拒绝请求。
- `relay/common/relay_info.go` 里和搜索一样设 `ForcePreConsume = true`，走同一条预扣和结算路径。
- 失败不写消费日志，预扣为 0，退款路径沿用搜索的分支（不收违规费）。
- 成功时的消费日志：`RelayInfo` 新增字段 `WebFetchLog map[string]any`，内容是 `host`、`path`（`EscapedPath`，截断到 256 字节，不含查询串）、`status`、`bytes`、`ms`。在 `service/text_quota.go` 的 `attachQuotaSaturation(ctx, relayInfo, other)` 之后加：

```go
if relayInfo.WebFetchLog != nil {
	other.SetPublic("web_fetch", relayInfo.WebFetchLog)
}
```

`X-Usage-Tag`、`X-Usage-Ref` 由 `RecordConsumeLog` 自动写入。失败的详情只进应用日志，因为数据库错误日志默认关闭（`ERROR_LOG_ENABLED=false`）。

## 9 渠道

- `constant/web_fetch.go`（新增）：

```go
package constant

const WebFetchModel = "web-fetch"

// IsToolChannel reports channels that serve a tool endpoint instead of LLM paths.
func IsToolChannel(channelType int) bool {
	return IsSearchChannel(channelType) || channelType == ChannelTypeWebFetch
}

func ToolChannelModels(channelType int) []string {
	if channelType == ChannelTypeWebFetch {
		return []string{WebFetchModel}
	}
	return SearchModels(channelType)
}
```

- 不要扩展 `IsSearchChannel`，否则 `web-fetch` 可能进入 `PerformSearch`。
- `model/channel_constraint.go` 的 `ValidateSearchConfiguration` 保留原名（现有测试引用它；创建、更新和按标签编辑渠道都经过它，因此自动覆盖 WebFetch）：入口条件改为 `IsToolChannel`；模型归属判断改为 `slices.Contains(constant.ToolChannelModels(ch.Type), name)`；末尾对 WebFetch 增加 `strings.TrimSpace(ch.GetSetting().Proxy) != ""` 时返回 `common.NewMessage("Web fetch channels do not support a proxy")`。在函数上加一行注释，说明它也校验 WebFetch。`ValidateSearchUpdate` 的入口条件同样改为 `IsToolChannel`。其余错误文案沿用。
- `channelMatchesFilter`：在 `/v1/search` 分支后加 `/v1/fetch` 分支，要求 `modelName == constant.WebFetchModel && ch.Type == constant.ChannelTypeWebFetch && ch.ValidateSearchConfiguration() == nil`；下面的隔离判断改为 `constant.IsToolChannel(ch.Type)`。
- Key：`validateChannel` 要求非空（`controller/channel.go:613`），后台填任意占位符，代码不读取。Base URL 留空。
- 后台渠道测试抓取 `https://example.com`，收到任何响应都算通过。

## 10 改动清单

| 文件 | 改动 |
|---|---|
| `constant/channel.go` | `ChannelTypeWebFetch = 68`，放在 `ChannelTypeDummy` 之前；`ChannelBaseURLs` 末尾补 `"", //68`；`ChannelTypeNames` 加 `"Web Fetch"` |
| `constant/web_fetch.go`（新） | 第 9 节 |
| `constant/endpoint_type.go`、`relaykit/types/endpoint_type.go` | `EndpointTypeWebFetch`，取值 `"web-fetch"` |
| `relaykit/types/relay_format.go` | `RelayFormatWebFetch = "web_fetch"` |
| `relaykit/dto/web_fetch.go`（新） | 第 4.2 节 |
| `common/endpoint_defaults.go` | `EndpointTypeWebFetch: {Path: "/v1/fetch", Method: "POST"}` |
| `common/endpoint_type.go` | `ChannelTypeWebFetch` 返回 `EndpointTypeWebFetch` |
| `common/api_type.go` | 条件改为 `constant.IsToolChannel` |
| `common/ssrf_protection.go` | 补三个网段 |
| `relay/constant/relay_mode.go` | 在枚举末尾加 `RelayModeWebFetch`；`path == "/v1/fetch"` 映射到它 |
| `relay/common/relay_info.go` | `GenRelayInfo` 加 `RelayFormatWebFetch` 分支（同搜索）；新增 `WebFetchLog` |
| `relay/helper/web_fetch_request.go`（新） | `GetAndValidateWebFetchRequest`，按 4.1 校验 |
| `relay/helper/valid_request.go` | 分发到上面的函数 |
| `relay/web_fetch_handler.go`（新） | 第 6 节 |
| `relay/request_billing.go` | 按次价格检查和退款分支都加 `RelayFormatWebFetch`；前者的错误用 `web_fetch.fixed_price_required` |
| `service/web_fetch.go`（新） | 第 5 节 |
| `service/token_counter.go` | `EstimateRequestToken` 的提前返回加 `RelayFormatWebFetch` |
| `service/text_quota.go` | 写 `other.web_fetch` |
| `setting/billing_setting/builtin_billing.go` | 内置价格 |
| `middleware/web_fetch_limit.go`（新） | 第 7 节 |
| `middleware/distributor.go` | 路径为 `/v1/fetch` 且 `model` 不是 `web-fetch` 时返回 `web_fetch.invalid_model` |
| `model/channel_constraint.go` | 第 9 节 |
| `controller/relay.go` | `RelayModeWebFetch` 分发到 `relay.WebFetchHelper` |
| `controller/channel-test.go` | 在搜索判断前加：`channel.Type == ChannelTypeWebFetch` 或端点为 `web-fetch` 时走新函数 `testWebFetchChannel`，结构照抄 `testSearchChannel`，调 `relay.PerformWebFetch` |
| `controller/channel_upstream_update.go` | 条件和返回改为 `IsToolChannel`、`ToolChannelModels` |
| `controller/model.go` | `openAIModels` 加 `web-fetch`；`channelId2Models` 的条件和取值改为 `IsToolChannel`、`ToolChannelModels` |
| `router/relay-router.go` | 第 7 节 |
| `i18n/keys.go`、`i18n/locales/{en,zh-CN,zh-TW}.yaml` | 第 4.3 节的 key，常量名形如 `MsgWebFetchInvalidURL` |
| `docs/openapi/relay.json` | 加 `/v1/fetch` 路径和 `WebFetchRequest`、`WebFetchResponse`，写法同 `/v1/search` |

非流式接口，不需要加入 `streamSupportedChannels`。

## 11 前端（`web/src/features/`）

对应 WebFetch 的受限表单和搜索渠道相同（隐藏模型映射、禁止手填模型、测试不走流式），所以直接加入现有集合。

| 文件 | 改动 |
|---|---|
| `channels/constants.ts` | `CHANNEL_TYPE_WEB_FETCH = 68`；加入 `SEARCH_CHANNEL_TYPES` 并注释说明原因；`CHANNEL_TYPES[68] = 'Web Fetch'`；`CHANNEL_PROVIDER_PRESENTATION[68]` 的 `descriptionKey` 为 `Fetch public web pages from this gateway. The key is not used; enter any placeholder.`；`CHANNEL_TYPE_DISPLAY_ORDER` 里放在 67 之后 |
| `channels/lib/channel-type-config.ts` | `supportedModels: ['web-fetch']`，其余同搜索渠道 |
| `channels/lib/channel-utils.ts` | `TYPE_TO_ICON` 加 68，取现有可用图标名（徽标因在 `SEARCH_CHANNEL_TYPES` 内，显示放大镜，不用改）；`TYPE_TO_KEY_PROMPT` 加 68，文案 `Enter any placeholder; this channel has no upstream key` |
| `channels/components/dialogs/channel-test-dialog.tsx` | 端点选项加 `{ value: 'web-fetch', label: 'Web Fetch (/v1/fetch)' }`；加入 `STREAM_INCOMPATIBLE_ENDPOINTS` |
| `pricing/constants.ts` | `ENDPOINT_TYPES.WEB_FETCH = 'web-fetch'`，标签 `t('Web Fetch')` |
| `pricing/components/model-details-api.tsx` | `buildSample` 加 `web-fetch` 分支，否则会落到聊天示例；示例参照 `buildSearchSample`，请求体为 `model`、`url` |
| `src/i18n/static-keys.ts`、七个 locale 文件 | 新文案，含后端的 `Web fetch channels do not support a proxy`；用 `bun run i18n:sync` |

扩展现有的 `channels/components/__tests__/provider-selection.test.tsx` 和 `channels/components/__tests__/channel-configuration.test.tsx`：能选到 Web Fetch，模型固定为 `web-fetch`，没有模型映射区块。

## 12 与需求草稿的差异

| 草稿 | 本方案 | 原因 |
|---|---|---|
| `url` 不超过 2048 字符 | 4000 | Cube 的 `WebFetchInput.url` 上限是 4000，不一致时长 URL 回退会用 `invalid_url` 盖掉原始错误 |
| 错误码表 | 增加 502 `unsupported_encoding` | 服务器未经协商返回 br 等编码时，正文无法使用 |
| 限流“沿用现有” | 429 带 JSON 错误体 | 现有 `writeRateLimited` 没有响应体，Cube 无法展示原因 |
| 多节点时可换节点重试 | 不做 | 现在只有一个出口，重试没有价值 |
| “可用 `net.Dialer.Control`” | 用 `ControlContext` | 同一机制，较新的接口 |
| 总并发上限未给数值 | 20 | 见第 7 节 |

草稿提到的 `docs/specs/relay-web-fetch.md` 在 Cube 仓库的所有分支上都不存在，以本文和草稿链接为准。

## 13 测试与发布

### 13.1 测试

遵循 `AGENTS.md` 的测试规则，全部集中在一个新文件 `controller/web_fetch_relay_test.go`，用 `require`/`assert`，不用 sleep。抓取行为直接调 `service.WebFetch` 并配合 `AllowWebFetchLoopbackForTest`，账务和路由用真实中间件链，脚手架参照 `controller/search_relay_test.go` 中建用户、Token、渠道的那条记账测试。

| 需求用例 | 做法与断言 |
|---|---|
| 1 普通页面 | httptest 返回 HTML，`status` 200，正文完整 |
| 2 二进制 | 返回 PNG 字节，经接口返回后 base64 解码完全一致 |
| 3 gzip | 服务端确认收到 `Accept-Encoding: gzip`，返回压缩内容，得到解压结果 |
| 4 三次重定向 | `url` 为最终地址 |
| 5 超过 5 次 | 第 5 次重定向仍成功，第 6 次起 `too_many_redirects` |
| 6 目标 404 | 接口 200，`status` 404 |
| 7 内网字面量 | 不开接缝，表驱动：`127.0.0.1`、`10.0.0.1`、`169.254.169.254`、`[::1]`、`[::ffff:127.0.0.1]`、`[2002:7f00:1::]`，都是 `blocked_address` |
| 8 域名解析到回环 | 不开接缝，`http://localhost/` 得 `blocked_address` |
| 9 公网跳内网 | 开接缝，httptest 重定向到 `http://10.0.0.1/` 和 `http://169.254.169.254/`，得 `blocked_address` |
| 10 URL 不合法 | 不开接缝，表驱动：`ftp://`、带用户名密码、`:8080`、超长，都是 `invalid_url`；重定向到非法地址同样 |
| 11 域名不存在 | `http://nonexistent.invalid/` 得 `dns_failed` |
| 12 超过 10 MB | 写入 10 MiB 加 1 字节，得 `too_large`；压缩后小、解压后超限的响应也一样 |
| 13 超时 | handler 等 `r.Context().Done()`，传入 50 ms 的 context，得 `timeout` |
| 14 请求头 | 走完整接口，带 `Authorization`、`X-Usage-*`、`Cookie`；目标只看到固定 `User-Agent`、`Accept`；重定向时响应 `Set-Cookie`，下一跳没有 `Cookie` 和 `Referer` |
| 15 鉴权和限流 | 无效 Key 401；第 31 次 429；用 channel 卡住 5 个请求，第 6 个 429，放行后恢复。窗口是进程级的，测试要用独立的 token id |
| 16 用量日志 | 带 `X-Usage-Ref`，消费日志有记录，`other.web_fetch` 不含查询串，`content` 里没有“no usage”，额度不变 |

另外覆盖：

- `httptest.NewTLSServer` 自签证书得 `connect_failed`（TLS 校验没有被关）。
- 关闭的服务得 `connect_failed`，`Content-Encoding: br` 得 `unsupported_encoding`，204 的 `body` 序列化为空字符串。
- 目标站失败时不重试、不停用渠道：配两个开启 AutoBan 的 WebFetch 渠道，handler 计数后 Hijack 断开连接，得 `connect_failed`，目标只收到一次请求，两个渠道状态都不变。
- 字段白名单和 `model` 校验。
- 渠道约束：`/v1/fetch` 只选 68 类型；68 类型不服务 `/v1/chat/completions` 和 `/v1/search`；`ValidateSearchConfiguration` 拒绝其他模型、映射、覆盖和 Proxy。

验证命令：

```bash
go test ./controller ./service ./relay/... ./middleware ./model ./common ./setting/...
(cd relaykit && GOWORK=off go build ./...)
(cd web && bun check -b && bun run lint && bun run test src/features/channels)
```

实施记录（2026-11-10）：抓取器直接调用独立 Transport 的 `RoundTrip`，由抓取循环解析重定向。失败日志仅记录目标主机、路径、错误码、耗时和错误类型。后台切换到 Web Fetch 时预填 `web-fetch`，清除不兼容的上游配置。定价页同步展示实际请求参数。

第 13.1 节的 Go 测试、relaykit 独立构建、前端类型检查和渠道测试通过，另验证了实际路由的鉴权和模型限流隔离。前端全量 lint 仍报告 141 条错误，与干净的 `main` 基线逐条一致。变更文件 lint 通过。

### 13.2 发布

按 `ops/deploy/RUNBOOK.md` 发布镜像。**发布前必须确认**源站入站只放行 Cloudflare 网段：`packapi.top` 在 Cloudflare 后面，而 `/v1/fetch` 从源站直连外网，持 Key 的人抓取自己的服务器就能看到源站 IP，草稿还要求对外提供出网 IP。如果源站可以被直接访问，先收紧安全组，或者改用独立 EIP 出网。

发布后在后台新建渠道：类型 Web Fetch，模型 `web-fetch`，Key 填占位符，分组包含 Cube Owner Key 所用分组，**关闭自动禁用**（否则定时测试偶发失败会停掉整个功能）。运行一次渠道测试。

线上验收：抓取 `https://github.com/mindshake-ai/cube` 得到完整 HTML；抓取 `http://169.254.169.254/latest/meta-data/` 得到 `blocked_address`；带 `X-Usage-Ref` 的调用在日志里可查且额度不变。把主机的出网 IP 记录给 Cube 侧，用于排查目标网站拦截。

## 14 Cube 对接（不在本次范围）

new-api 发布后再做，改 `cube/extensions/web/tools/_client.py`：

- 仅在解析失败、连接失败、TLS 失败或超时时调用 `/v1/fetch`；目标已返回状态码或地址是内网时不调用。
- 用空间 Owner 的 Key，带与 `/v1/search` 相同的 `X-Usage-Tag`、`X-Usage-Ref`。
- 给本地连接阶段设约 10 秒的短超时，否则 Agent 最长要等 60 秒以上；请求中转站的超时要大于 30 秒。
- 拿到 `status` 后按本地逻辑处理（非 2xx 报错）；`body` base64 解码，从 `content_type` 解析媒体类型和编码，之后走现有内容处理。

## 15 不做

JS 渲染、登录态、cookie、POST、自定义请求头、缓存、正文提取、第三方抓取服务、多节点出口。
