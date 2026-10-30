package controller

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	hostdto "github.com/QuantumNous/new-api/dto"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/middleware"
	"github.com/QuantumNous/new-api/model"
	"github.com/QuantumNous/new-api/relay"
	relaycommon "github.com/QuantumNous/new-api/relay/common"
	"github.com/QuantumNous/new-api/relay/helper"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/QuantumNous/new-api/service"
	"github.com/QuantumNous/new-api/setting/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func newSearchTestContext(t *testing.T, body string, channelType int, baseURL string) (*gin.Context, *relaycommon.RelayInfo) {
	t.Helper()
	require.NoError(t, i18n.Init())
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/search", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("Accept-Language", "en")
	c.Request.Header.Set("Authorization", "Bearer downstream-secret")
	request, err := helper.GetAndValidateSearchRequest(c)
	require.NoError(t, err)
	common.SetContextKey(c, constant.ContextKeyOriginalModel, request.Model)
	common.SetContextKey(c, constant.ContextKeyChannelType, channelType)
	common.SetContextKey(c, constant.ContextKeyChannelBaseUrl, baseURL)
	common.SetContextKey(c, constant.ContextKeyChannelKey, "upstream-secret")
	common.SetContextKey(c, constant.ContextKeyUsingGroup, "default")
	common.SetContextKey(c, constant.ContextKeyUserGroup, "default")
	c.Set(common.RequestIdKey, "search-request")
	info, err := relaycommon.GenRelayInfo(c, types.RelayFormatSearch, request, nil)
	require.NoError(t, err)
	return c, info
}

func TestUnifiedSearchProviderRequestsAndResponses(t *testing.T) {
	service.InitHttpClient()
	for _, tc := range []struct {
		model                  string
		channelType            int
		method, path, response string
	}{
		{"tavily-search", constant.ChannelTypeTavily, "POST", "/search", `{"results":[{"title":"Title","url":"https://example.com","content":"Snippet","published_date":"2026-10-29"}]}`},
		{"tavily-search-advanced", constant.ChannelTypeTavily, "POST", "/search", `{"results":[{"title":"Title","url":"https://example.com","content":"Snippet"}]}`},
		{"brave-search", constant.ChannelTypeBraveSearch, "GET", "/res/v1/web/search", `{"query":{"original":"query"},"web":{"results":[{"title":"Title","url":"https://example.com","description":"Snippet","profile":{"name":"Example"}}]}}`},
		{"bocha-search", constant.ChannelTypeBochaSearch, "POST", "/v1/web-search", `{"code":200,"data":{"webPages":{"value":[{"name":"Title","url":"https://example.com","snippet":"Snippet","siteName":"Example","datePublished":"2026-10-29"}]}}}`},
		{"baidu-search", constant.ChannelTypeBaiduSearch, "POST", "/v2/ai_search/web_search", `{"request_id":"baidu-request","references":[{"title":"Title","url":"https://example.com","content":"Snippet","website":"Example","date":"2026-10-30","type":"web"}]}`},
	} {
		t.Run(tc.model, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				assert.Equal(t, tc.method, r.Method)
				assert.Equal(t, tc.path, r.URL.Path)
				assert.Empty(t, r.Header.Get("mj-api-secret"), "the caller's compatibility credential must not be forwarded")
				if tc.channelType == constant.ChannelTypeBraveSearch {
					assert.Equal(t, "upstream-secret", r.Header.Get("X-Subscription-Token"))
					assert.Empty(t, r.Header.Get("Authorization"))
					assert.Equal(t, "New API 中文", r.URL.Query().Get("q"))
					assert.Equal(t, "5", r.URL.Query().Get("count"))
					assert.Equal(t, "pw", r.URL.Query().Get("freshness"))
					assert.Equal(t, "web", r.URL.Query().Get("result_filter"))
				} else {
					assert.Equal(t, "Bearer upstream-secret", r.Header.Get("Authorization"))
					assert.Empty(t, r.Header.Get("X-Subscription-Token"))
					raw, err := io.ReadAll(r.Body)
					require.NoError(t, err)
					var body map[string]any
					require.NoError(t, common.Unmarshal(raw, &body))
					if tc.channelType != constant.ChannelTypeBaiduSearch {
						assert.Equal(t, "New API 中文", body["query"])
					}
					assert.NotContains(t, body, "model")
					assert.NotContains(t, body, "api_key")
					if tc.channelType == constant.ChannelTypeTavily {
						depth := "basic"
						if tc.model == "tavily-search-advanced" {
							depth = "advanced"
						}
						assert.Equal(t, depth, body["search_depth"])
						assert.Equal(t, false, body["auto_parameters"])
						assert.Equal(t, false, body["include_answer"])
						assert.Equal(t, false, body["include_raw_content"])
						assert.Equal(t, "week", body["time_range"])
						assert.Equal(t, float64(5), body["max_results"])
					} else if tc.channelType == constant.ChannelTypeBaiduSearch {
						assert.Equal(t, "standard", body["edition"])
						assert.Equal(t, "baidu_search_v2", body["search_source"])
						assert.Equal(t, "week", body["search_recency_filter"])
						assert.Equal(t, []any{map[string]any{"role": "user", "content": "New API 中文"}}, body["messages"])
						assert.Equal(t, []any{map[string]any{"type": "web", "top_k": float64(5)}}, body["resource_type_filter"])
						assert.NotContains(t, body, "query")
						assert.NotContains(t, body, "stream")
					} else {
						assert.Equal(t, "oneWeek", body["freshness"])
						assert.Equal(t, false, body["summary"])
						assert.Equal(t, float64(5), body["count"])
					}
				}
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, tc.response)
			}))
			defer server.Close()
			c, info := newSearchTestContext(t, `{"model":"`+tc.model+`","query":"New API 中文","freshness":"week"}`, tc.channelType, server.URL)
			c.Request.Header.Set("mj-api-secret", "downstream-secret")
			common.SetContextKey(c, constant.ContextKeyChannelHeaderOverride, map[string]any{"*": ""})
			response, apiErr := relay.PerformSearch(c, info)
			require.Nil(t, apiErr)
			assert.Equal(t, 1, calls)
			assert.Equal(t, "search-request", response.ID)
			assert.Equal(t, tc.model, response.Model)
			assert.Equal(t, "New API 中文", response.Query)
			require.Len(t, response.Results, 1)
			assert.Equal(t, "Title", response.Results[0].Title)
			assert.Equal(t, "https://example.com", response.Results[0].URL)
			assert.Equal(t, "Snippet", response.Results[0].Content)
			if tc.channelType == constant.ChannelTypeBaiduSearch {
				assert.Equal(t, "Example", response.Results[0].SiteName)
				assert.Equal(t, "2026-10-30", response.Results[0].PublishedDate)
			}
		})
	}
}

func TestUnifiedSearchValidation(t *testing.T) {
	require.NoError(t, i18n.Init())
	cases := []string{
		`null`, `{}`, `{"model":"unknown","query":"hi"}`,
		`{"model":"tavily-search","query":"  "}`,
		`{"model":"tavily-search","query":"hi","max_results":0}`,
		`{"model":"tavily-search","query":"hi","max_results":-1}`,
		`{"model":"tavily-search","query":"hi","max_results":21}`,
		`{"model":"tavily-search","query":"hi","max_results":1.5}`,
		`{"model":"tavily-search","query":"hi","max_results":18446744073709551615}`,
		`{"model":"tavily-search","query":"hi","freshness":"anything"}`,
		`{"model":"tavily-search","query":"hi","stream":false}`,
		`{"model":"tavily-search","query":"hi","auto_parameters":true}`,
		`{"model":"tavily-search","query":"hi","search_depth":"advanced"}`,
		`{"model":"tavily-search","query":"hi","include_raw_content":true}`,
		`{"model":"tavily-search","query":"` + strings.Repeat("字", 401) + `"}`,
		`{"model":"tavily-search","query":"` + strings.Repeat("word ", 51) + `"}`,
		`{"model":"baidu-search","query":"hi","freshness":"day"}`,
		`{"model":"baidu-search","query":"hi","edition":"pro"}`,
	}
	for _, body := range cases {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/search", strings.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		_, err := helper.GetAndValidateSearchRequest(c)
		assert.Error(t, err, body)
	}
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/search", strings.NewReader(`{"model":"tavily-search","query":"hi"}`))
	_, err := helper.GetAndValidateSearchRequest(c)
	assert.Error(t, err, "requires JSON content type")
}

func TestUnifiedSearchEmptyAndMalformedResponses(t *testing.T) {
	service.InitHttpClient()
	for _, tc := range []struct {
		name          string
		channelType   int
		product, body string
		status        int
		success       bool
	}{
		{"tavily empty", constant.ChannelTypeTavily, "tavily-search", `{"results":[]}`, 200, true},
		{"brave omitted web", constant.ChannelTypeBraveSearch, "brave-search", `{"query":{"original":"hi"}}`, 200, true},
		{"bocha empty", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":200,"data":{"webPages":{"value":[]}}}`, 200, true},
		{"bocha omitted webPages", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":200,"data":{}}`, 200, true},
		{"bocha null webPages", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":200,"data":{"webPages":null}}`, 200, false},
		{"bocha missing data", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":200}`, 200, false},
		{"baidu empty", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request","references":[]}`, 200, true},
		{"baidu omitted references", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request"}`, 200, true},
		{"baidu null references", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request","references":null}`, 200, false},
		{"baidu missing request id", constant.ChannelTypeBaiduSearch, "baidu-search", `{"references":[]}`, 200, false},
		{"baidu logical error", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request","code":"InvalidRequest","message":"upstream-secret","references":[]}`, 200, false},
		{"baidu error message", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request","message":"upstream-secret","references":[]}`, 200, false},
		{"baidu invalid metadata", constant.ChannelTypeBaiduSearch, "baidu-search", `{"request_id":"baidu-request","references":[{"title":"Title","url":"https://example.com","website":{}}]}`, 200, false},
		{"HTTP error secret is hidden", constant.ChannelTypeTavily, "tavily-search", `{"error":"upstream-secret downstream-secret"}`, 429, false},
		{"malformed JSON", constant.ChannelTypeTavily, "tavily-search", `{"results":`, 200, false},
		{"wrong success shape", constant.ChannelTypeTavily, "tavily-search", `{}`, 200, false},
		{"bad result", constant.ChannelTypeTavily, "tavily-search", `{"results":[{"title":"no URL"}]}`, 200, false},
		{"non-string title", constant.ChannelTypeTavily, "tavily-search", `{"results":[{"title":{"unexpected":true},"url":"https://example.com","content":"summary"}]}`, 200, false},
		{"string bocha code", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":"200","data":{"webPages":{"value":[]}}}`, 200, false},
		{"logical bocha error", constant.ChannelTypeBochaSearch, "bocha-search", `{"code":500,"msg":"upstream-secret"}`, 200, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			}))
			defer server.Close()
			c, info := newSearchTestContext(t, `{"model":"`+tc.product+`","query":"hi"}`, tc.channelType, server.URL)
			response, apiErr := relay.PerformSearch(c, info)
			if tc.success {
				require.Nil(t, apiErr)
				require.NotNil(t, response.Results)
				assert.Empty(t, response.Results)
				return
			}
			require.NotNil(t, apiErr)
			assert.NotContains(t, apiErr.Error(), "upstream-secret")
			assert.NotContains(t, apiErr.Error(), "downstream-secret")
			expected := tc.status
			if expected == 200 {
				expected = 502
			}
			assert.Equal(t, expected, apiErr.StatusCode)
		})
	}
}

func TestBaiduSearchWebResultsAndFreshness(t *testing.T) {
	service.InitHttpClient()
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]any
		require.NoError(t, common.DecodeJson(r.Body, &body))
		assert.NotContains(t, body, "search_recency_filter", "omitted freshness must remain unrestricted")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"request_id":"baidu-request","references":[{"type":"image","url":"https://example.com/image","title":"Image"},{"type":"web","is_aladdin":true,"url":"https://example.com/card","title":"Card"},{"type":"web","url":"https://example.com/first","title":"First","snippet":"Summary","content":"Long content"},{"url":"https://example.com/second","title":"Second","content":"Fallback"},{"type":"web","url":"https://example.com/third","title":"Third","content":"Extra"}]}`)
	}))
	defer server.Close()
	c, info := newSearchTestContext(t, `{"model":"baidu-search","query":"中文检索","max_results":2}`, constant.ChannelTypeBaiduSearch, server.URL)
	response, apiErr := relay.PerformSearch(c, info)
	require.Nil(t, apiErr)
	require.Len(t, response.Results, 2)
	assert.Equal(t, "https://example.com/first", response.Results[0].URL)
	assert.Equal(t, "Summary", response.Results[0].Content)
	assert.Equal(t, "https://example.com/second", response.Results[1].URL)
	assert.Equal(t, "Fallback", response.Results[1].Content)
	info.Request.(*dto.SearchRequest).Freshness = common.GetPointer("day")
	_, apiErr = relay.PerformSearch(c, info)
	require.NotNil(t, apiErr)
	assert.Equal(t, http.StatusBadRequest, apiErr.StatusCode)
	assert.Equal(t, 1, calls, "unsupported freshness must fail before upstream even in administrator tests")
}

func TestUnifiedSearchRejectsRedirectAndProductOverrides(t *testing.T) {
	service.InitHttpClient()
	forwarded := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { forwarded++ }))
	defer target.Close()
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	c, info := newSearchTestContext(t, `{"model":"brave-search","query":"hi"}`, constant.ChannelTypeBraveSearch, server.URL)
	_, apiErr := relay.PerformSearch(c, info)
	require.NotNil(t, apiErr)
	assert.Equal(t, 1, calls)
	assert.Zero(t, forwarded, "credentials must never follow redirects")
	for _, tc := range []struct {
		mapping     string
		override    map[string]any
		channelType int
	}{
		{`{"tavily-search":"tavily-search-advanced"}`, nil, constant.ChannelTypeTavily},
		{`{"tavily-search":"brave-search"}`, nil, constant.ChannelTypeTavily},
		{"", map[string]any{"search_depth": "advanced"}, constant.ChannelTypeTavily},
		{"", nil, constant.ChannelTypeBraveSearch},
	} {
		c, info := newSearchTestContext(t, `{"model":"tavily-search","query":"hi"}`, tc.channelType, server.URL)
		c.Set("model_mapping", tc.mapping)
		common.SetContextKey(c, constant.ContextKeyChannelParamOverride, tc.override)
		_, apiErr := relay.PerformSearch(c, info)
		require.NotNil(t, apiErr)
	}
	assert.Equal(t, 1, calls, "invalid configurations must fail before upstream")
}

func TestUnifiedSearchChannelSelectionAndCatalog(t *testing.T) {
	filters := []hostdto.ChannelFilter{{Kind: hostdto.FilterRequestPath, RequestPath: "/v1/search"}}
	channel := &model.Channel{Type: constant.ChannelTypeTavily, Models: "tavily-search,tavily-search-advanced"}
	allowed, _ := model.ChannelSatisfiesFilters(channel, "tavily-search", filters)
	assert.True(t, allowed)
	allowed, _ = model.ChannelSatisfiesFilters(channel, "brave-search", filters)
	assert.False(t, allowed)
	allowed, _ = model.ChannelSatisfiesFilters(channel, "tavily-search", []hostdto.ChannelFilter{{Kind: hostdto.FilterRequestPath, RequestPath: "/v1/chat/completions"}})
	assert.False(t, allowed)
	channel.ModelMapping = common.GetPointer(`{"tavily-search":"tavily-search-advanced"}`)
	assert.Error(t, channel.ValidateSearchConfiguration())
	allowed, _ = model.ChannelSatisfiesFilters(channel, "tavily-search", filters)
	assert.False(t, allowed)
	for _, channelType := range []int{constant.ChannelTypeTavily, constant.ChannelTypeBraveSearch, constant.ChannelTypeBochaSearch, constant.ChannelTypeBaiduSearch} {
		models, err := fetchChannelUpstreamModelIDs(&model.Channel{Type: channelType, BaseURL: common.GetPointer("https://must-not-be-called.invalid")})
		require.NoError(t, err)
		assert.Equal(t, constant.SearchModels(channelType), models)
		assert.Equal(t, models, channelId2Models[channelType])
	}
}

func TestSearchRelayAccessAndAccounting(t *testing.T) {
	user, token := setupResponsesWSRequestTest(t)
	require.NoError(t, i18n.Init())
	service.InitHttpClient()
	previousConfig := config.GlobalConfig.ExportAllConfigs()
	previousBatch, previousLogs, previousCount := common.BatchUpdateEnabled, common.LogConsumeEnabled, constant.CountToken
	previousQuota, previousRetries := common.QuotaPerUnit, common.RetryTimes
	common.BatchUpdateEnabled, common.LogConsumeEnabled, constant.CountToken = false, true, false
	common.QuotaPerUnit, common.RetryTimes = 500000, 1
	t.Cleanup(func() {
		require.NoError(t, config.GlobalConfig.LoadFromDB(previousConfig))
		common.BatchUpdateEnabled, common.LogConsumeEnabled, constant.CountToken = previousBatch, previousLogs, previousCount
		common.QuotaPerUnit, common.RetryTimes = previousQuota, previousRetries
	})
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{
		"billing_setting.billing_mode":    `{"tavily-search":"tiered_expr","gpt-search-test":"tiered_expr"}`,
		"billing_setting.billing_expr":    `{"tavily-search":"tier(\"request\", fixed(0.002))","gpt-search-test":"tier(\"request\", fixed(0.001))"}`,
		"group_ratio_setting.group_ratio": `{"default":2}`,
		"quota_setting.trust_quota_usd":   "0",
		"perf_metrics_setting.enabled":    "false",
	}))
	require.NoError(t, model.DB.AutoMigrate(&model.Channel{}, &model.Ability{}))
	require.NoError(t, model.LOG_DB.AutoMigrate(&model.Log{}))
	require.NoError(t, model.DB.Model(user).Updates(map[string]any{"quota": 100000, "setting": `{"billing_preference":"wallet_only"}`}).Error)
	require.NoError(t, model.DB.Model(token).Update("remain_quota", 100000).Error)

	var responseMode, primaryCalls, fallbackCalls, wrongCalls, baiduCalls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "Bearer upstream-secret", r.Header.Get("Authorization"))
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/primary/search":
			primaryCalls.Add(1)
			if responseMode.Load() >= 2 {
				w.WriteHeader(http.StatusBadGateway)
				_, _ = w.Write([]byte(`{"error":"upstream-secret must not escape"}`))
				return
			}
			if responseMode.Load() == 1 {
				_, _ = w.Write([]byte(`{"results":[]}`))
				return
			}
			_, _ = w.Write([]byte(`{"results":[{"title":"Result","url":"https://example.com","content":"Summary"}]}`))
		case "/fallback/search":
			fallbackCalls.Add(1)
			if responseMode.Load() == 3 {
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			_, _ = w.Write([]byte(`{"results":[]}`))
		case "/v1/chat/completions":
			_, _ = w.Write([]byte(`{"id":"chat-test","object":"chat.completion","model":"gpt-search-test","choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
		case "/v2/ai_search/web_search":
			baiduCalls.Add(1)
			switch responseMode.Load() {
			case 1:
				_, _ = io.WriteString(w, `{"request_id":"baidu-empty"}`)
			case 4:
				_, _ = io.WriteString(w, `{"request_id":"baidu-error","code":"InternalError","message":"upstream-secret"}`)
			default:
				_, _ = io.WriteString(w, `{"request_id":"baidu-success","references":[{"title":"Result","url":"https://example.com","content":"Summary"}]}`)
			}
		default:
			wrongCalls.Add(1)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	t.Cleanup(upstream.Close)
	for _, item := range []struct {
		name, models, path string
		kind               int
		priority           int64
	}{
		{"primary", "tavily-search", "/primary", constant.ChannelTypeTavily, 10},
		{"fallback", "tavily-search", "/fallback", constant.ChannelTypeTavily, 5},
		{"wrong-provider", "tavily-search", "/wrong", constant.ChannelTypeBraveSearch, 100},
		{"chat", "gpt-search-test", "", constant.ChannelTypeOpenAI, 0},
		{"baidu", "baidu-search", "", constant.ChannelTypeBaiduSearch, 0},
	} {
		baseURL := upstream.URL + item.path
		channel := &model.Channel{Name: item.name, Models: item.models, Type: item.kind, BaseURL: &baseURL, Key: "upstream-secret", Group: "default", Status: common.ChannelStatusEnabled, Priority: common.GetPointer(item.priority), AutoBan: common.GetPointer(0)}
		require.NoError(t, model.DB.Create(channel).Error)
		require.NoError(t, channel.AddAbilities(nil))
	}
	engine := gin.New()
	engine.Use(func(c *gin.Context) { c.Set(common.RequestIdKey, "search-accounting"); c.Next() })
	engine.POST("/v1/search", middleware.TokenAuth(), middleware.ModelRequestRateLimit(), middleware.Distribute(), func(c *gin.Context) { Relay(c, types.RelayFormatSearch) })
	engine.POST("/v1/chat/completions", middleware.TokenAuth(), middleware.ModelRequestRateLimit(), middleware.Distribute(), func(c *gin.Context) { Relay(c, types.RelayFormatOpenAI) })
	send := func(path, body, key string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Accept-Language", "en")
		if key != "" {
			request.Header.Set("Authorization", "Bearer sk-"+key)
		}
		recorder := httptest.NewRecorder()
		engine.ServeHTTP(recorder, request)
		return recorder
	}
	search := `{"model":"tavily-search","query":"renewable energy"}`
	assert.Equal(t, http.StatusUnauthorized, send("/v1/search", search, "").Code)
	require.NoError(t, model.DB.Model(token).Updates(map[string]any{"model_limits_enabled": true, "model_limits": "gpt-search-test"}).Error)
	assert.Equal(t, http.StatusForbidden, send("/v1/search", search, token.Key).Code)
	require.NoError(t, model.DB.Model(token).Updates(map[string]any{"model_limits_enabled": false, "remain_quota": 500}).Error)
	assert.Equal(t, http.StatusForbidden, send("/v1/search", search, token.Key).Code)
	require.NoError(t, model.DB.Model(token).Update("remain_quota", 100000).Error)
	require.NoError(t, model.DB.Model(user).Update("quota", 500).Error)
	assert.Equal(t, http.StatusForbidden, send("/v1/search", search, token.Key).Code)
	require.NoError(t, model.DB.Model(user).Update("quota", 100000).Error)
	assert.Zero(t, primaryCalls.Load(), "rejected requests must not reach upstream")

	chat := send("/v1/chat/completions", `{"model":"gpt-search-test","messages":[{"role":"user","content":"hi"}]}`, token.Key)
	require.Equal(t, http.StatusOK, chat.Code, chat.Body.String())
	for mode := range int32(3) {
		responseMode.Store(mode)
		response := send("/v1/search", search, token.Key)
		require.Equal(t, http.StatusOK, response.Code, response.Body.String())
		var body dto.SearchResponse
		require.NoError(t, common.Unmarshal(response.Body.Bytes(), &body))
		assert.Equal(t, "tavily-search", body.Model)
		require.NotNil(t, body.Results)
		if mode > 0 {
			assert.Empty(t, body.Results)
		}
	}
	assert.EqualValues(t, 3, primaryCalls.Load())
	assert.EqualValues(t, 1, fallbackCalls.Load())
	assert.Zero(t, wrongCalls.Load(), "a higher priority channel from another provider must never be used")
	var storedUser model.User
	var storedToken model.Token
	require.NoError(t, model.DB.First(&storedUser, user.Id).Error)
	require.NoError(t, model.DB.First(&storedToken, token.Id).Error)
	assert.Equal(t, 93000, storedUser.Quota)
	assert.Equal(t, 93000, storedToken.RemainQuota)
	assert.Equal(t, 7000, storedUser.UsedQuota)
	assert.Equal(t, 7000, storedToken.UsedQuota)

	responseMode.Store(3)
	failed := send("/v1/search", search, token.Key)
	assert.GreaterOrEqual(t, failed.Code, 400)
	assert.NotContains(t, failed.Body.String(), "upstream-secret")
	// Refunds run asynchronously in the shared billing session.
	require.Eventually(t, func() bool {
		if model.DB.First(&storedUser, user.Id).Error != nil || model.DB.First(&storedToken, token.Id).Error != nil {
			return false
		}
		return storedUser.Quota == 93000 && storedToken.RemainQuota == 93000
	}, 3*time.Second, 10*time.Millisecond)
	assert.Equal(t, 7000, storedToken.UsedQuota)

	before := primaryCalls.Load()
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{"billing_setting.billing_mode": `{}`, "billing_setting.billing_expr": `{}`}))
	assert.Equal(t, http.StatusBadRequest, send("/v1/search", search, token.Key).Code)
	assert.Equal(t, before, primaryCalls.Load(), "missing prices must be rejected before upstream")
	var primary model.Channel
	require.NoError(t, model.DB.Where("name = ?", "primary").First(&primary).Error)
	unpricedTest := testChannel(nil, &primary, user.Id, "tavily-search", "search", false)
	require.Error(t, unpricedTest.localErr)
	assert.Equal(t, before, primaryCalls.Load(), "administrator tests must also require pricing")
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{
		"billing_setting.billing_mode": `{"tavily-search":"tiered_expr"}`,
		"billing_setting.billing_expr": `{"tavily-search":"tier(\"request\", fixed(0))"}`,
	}))
	responseMode.Store(1)
	free := send("/v1/search", search, token.Key)
	require.Equal(t, http.StatusOK, free.Code, free.Body.String())
	require.NoError(t, model.DB.First(&storedUser, user.Id).Error)
	require.NoError(t, model.DB.First(&storedToken, token.Id).Error)
	assert.Equal(t, 93000, storedUser.Quota)
	assert.Equal(t, 93000, storedToken.RemainQuota)
	var logs []model.Log
	require.NoError(t, model.LOG_DB.Where("token_id = ? AND type = ?", token.Id, model.LogTypeConsume).Find(&logs).Error)
	require.Len(t, logs, 5, "one chat, three charged searches and one explicit free search")
	for _, log := range logs {
		if log.ModelName == "tavily-search" {
			assert.Zero(t, log.PromptTokens)
			assert.Zero(t, log.CompletionTokens)
			assert.Contains(t, log.Other, `"billing_unit":"request"`)
			var details map[string]any
			require.NoError(t, common.UnmarshalJsonStr(log.Other, &details))
			require.Contains(t, details, "frt")
			assert.GreaterOrEqual(t, details["frt"].(float64), float64(0))
		}
		assert.NotContains(t, log.Other, "upstream-secret")
	}
	adminTest := testChannel(nil, &primary, user.Id, "tavily-search", "search", false)
	require.NoError(t, adminTest.localErr)
	require.Nil(t, adminTest.newAPIError)
	before = primaryCalls.Load()
	primary.ParamOverride = common.GetPointer(`{"search_depth":"advanced"}`)
	overriddenTest := testChannel(nil, &primary, user.Id, "tavily-search", "search", false)
	require.Error(t, overriddenTest.localErr)
	assert.Equal(t, before, primaryCalls.Load(), "administrator tests cannot bypass product restrictions")

	baiduRequest := `{"model":"baidu-search","query":"新能源"}`
	assert.Equal(t, http.StatusBadRequest, send("/v1/search", baiduRequest, token.Key).Code)
	assert.Zero(t, baiduCalls.Load(), "Baidu also requires an explicitly configured price")
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{
		"billing_setting.billing_mode": `{"baidu-search":"tiered_expr"}`,
		"billing_setting.billing_expr": `{"baidu-search":"tier(\"base\", fixed(0.003))"}`,
	}))
	assert.Equal(t, http.StatusBadRequest, send("/v1/search", `{"model":"baidu-search","query":"新能源","freshness":"day"}`, token.Key).Code)
	assert.Zero(t, baiduCalls.Load())
	for mode := range int32(2) {
		responseMode.Store(mode)
		response := send("/v1/search", baiduRequest, token.Key)
		require.Equal(t, http.StatusOK, response.Code, response.Body.String())
	}
	responseMode.Store(4)
	failed = send("/v1/search", baiduRequest, token.Key)
	assert.Equal(t, http.StatusBadGateway, failed.Code)
	assert.NotContains(t, failed.Body.String(), "upstream-secret")
	require.Eventually(t, func() bool {
		if model.DB.First(&storedUser, user.Id).Error != nil || model.DB.First(&storedToken, token.Id).Error != nil {
			return false
		}
		return storedUser.Quota == 87000 && storedToken.RemainQuota == 87000
	}, 3*time.Second, 10*time.Millisecond)
	var baiduLogs []model.Log
	require.NoError(t, model.LOG_DB.Where("token_id = ? AND type = ? AND model_name = ?", token.Id, model.LogTypeConsume, "baidu-search").Find(&baiduLogs).Error)
	require.Len(t, baiduLogs, 2, "one result and one empty success; logical errors must refund")
	for _, log := range baiduLogs {
		assert.Equal(t, 3000, log.Quota)
		assert.Contains(t, log.Other, `"billing_unit":"request"`)
	}
}
