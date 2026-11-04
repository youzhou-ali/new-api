# 统一搜索接口支持限定网站

日期：2026-11-04。状态：已实施并本地提交 `1aaea27d`，Go 编译与测试通过，已发布 packapi；三家已上架搜索源真实调用通过，部分上游行为待验证。

在 [0001](0001-search-relay.md) 的 `POST /v1/search` 上增加可选字段 `domains`，只返回这些网站的结果。调用方是 Cube 的 `WebSearch` 工具：它的 `domains` 参数原来只能交给直连的搜索服务商，内置搜索改走本接口后，由本接口提供同样的能力。Cube 一侧的方案见 cube 仓库 `docs/design/plans/web-access-plan.md` 第 14 节。

**请求。** `domains` 是字符串数组，省略或为空数组表示不限，最多 20 个（按博查资料里较保守的上限）。每项是主机名：先转小写、去掉末尾的点，然后只能含字母、数字、连字符和点，至少有一个点；不带协议、路径、端口和通配符，国际化域名用 punycode。去重后发给上游。不合规时返回 400，不重试，也不扣费。字段白名单在 `model`、`query`、`max_results`、`freshness` 之外加上 `domains`，其他字段仍然拒绝。

**映射。**

| 产品 | 上游参数 |
| --- | --- |
| `tavily-search`、`tavily-search-advanced` | 请求体 `include_domains: [...]` |
| `brave-search` | 查询参数 `goggles`，写内联规则：第一行 `$discard`，之后每个网站一行 `$boost,site=<主机名>`，各行用换行连接 |
| `bocha-search` | 请求体 `include: "a.com\|b.com"` |
| `baidu-search` | 请求体 `search_filter: {"match": {"site": [...]}}` |

不带 `domains` 时，发给上游的请求和现在完全相同。

**结果按主机名再过滤一遍。** 结果 URL 的主机名等于某个 `domains` 项，或以 `.<该项>` 结尾，才保留。各家对子域名的处理不一致，再过滤一遍才能保证结果里不出现列表以外的网站。过滤后条数可能少于 `max_results`，甚至为空；空结果仍按 0001 算一次成功搜索。

**计费不变。** `domains` 不是计费倍率，每次搜索仍按产品的每次价格收费。百度的 `site` 过滤是「限时免费」的付费功能，百度开始收费后要重新定价。

**改动位置。**

- `relaykit/dto/search.go`：`SearchRequest` 加 `Domains []string` 字段（`json:"domains,omitempty"`）。DTO 只描述协议，校验放在宿主。改完在 `relaykit/` 里跑 `GOWORK=off go build ./...`。
- `relay/helper/search_request.go`：放开 `domains` 并按上文校验。
- `relay/search_handler.go`：`PerformSearch` 按渠道映射；`normalizeSearchResults` 之后按主机名过滤，再截到 `max_results`。
- `docs/openapi/relay.json`：补字段说明。
- `i18n`：加一条 `domains` 不合规的错误文案（en、zh-CN、zh-TW）。

**测试。** 扩展现有的 `controller/search_relay_test.go`，不新开文件：

- 四类渠道各一条，断言发给上游的请求：Tavily、博查、百度的请求体，Brave 的 `goggles` 查询参数。
- 不带 `domains` 时，上游请求和现在相同。
- 不合规的项（不是数组、带协议、带路径、带端口、通配符、空字符串、没有点、非 ASCII、超过 20 个）被拒绝。校验在预扣费之前，所以不扣费。
- 上游返回列表以外的网站时被过滤掉。

**实测与待验证。** 2026-11-04 packapi 发布后，使用专用 QA Token 对百度、博查、Tavily 各发一次带 `domains` 和一次不带的请求。限定 `python.org` 时最终结果都在其子域名下；六笔消费每次 $0.004，博查实际按计费表达式而非旧 `model_price` 扣费。日志证据见 Cube `docs/validation/web-search-relay-2026-11-04.md`。下列问题仍待验证：

- Brave API 是否接受内联 Goggles 白名单。官方 API 文档只写了排除（`$discard,site=`）和提权（`$boost`），白名单写法出自 Goggles 快速入门。
- 博查 `include` 的上限，资料里有 20 和 100 两种说法。
- 百度上游 `site` 参数本身是否生效、是否另行计费（最终结果还经过中转站过滤，不能单凭它判断上游效果）。
- 各源上游对子域名的处理，以及真实上游返回仅后缀相同域名时中转站的过滤。
