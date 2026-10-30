package relay

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/relay/channel"
	relaycommon "github.com/QuantumNous/new-api/relay/common"
	"github.com/QuantumNous/new-api/relay/helper"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/QuantumNous/new-api/service"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
)

func SearchHelper(c *gin.Context, info *relaycommon.RelayInfo) *types.NewAPIError {
	response, apiErr := PerformSearch(c, info)
	if apiErr != nil {
		return apiErr
	}
	// A valid upstream success, including an empty results array, is one call.
	// The fixed-price snapshot settles only once after any channel retries.
	service.PostTextConsumeQuota(c, info, &dto.Usage{}, nil)
	c.JSON(http.StatusOK, response)
	return nil
}

// PerformSearch is shared by the relay and the administrator's channel test.
// It performs no accounting and does not commit a downstream response.
func PerformSearch(c *gin.Context, info *relaycommon.RelayInfo) (*dto.SearchResponse, *types.NewAPIError) {
	info.InitChannelMeta(c)
	request, ok := info.Request.(*dto.SearchRequest)
	if !ok || request.Model != info.OriginModelName || constant.SearchChannelType(request.Model) != info.ChannelType || !constant.IsSearchChannel(info.ChannelType) {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgSearchInvalidChannel)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}
	if err := helper.ModelMappedHelper(c, info, nil); err != nil || info.UpstreamModelName != info.OriginModelName || len(info.ParamOverride) != 0 || info.ChannelSetting.PassThroughBodyEnabled {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgSearchChannelOverrides)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}
	if request.MaxResults == nil || *request.MaxResults < 1 || *request.MaxResults > 20 {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgSearchInvalidCount)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}

	baseURL, err := url.Parse(strings.TrimRight(info.ChannelBaseUrl, "/"))
	if err != nil || baseURL.Host == "" || (baseURL.Scheme != "https" && baseURL.Scheme != "http") || baseURL.User != nil || baseURL.RawQuery != "" || baseURL.Fragment != "" {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgSearchInvalidBaseURL)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}
	method := http.MethodPost
	freshness := ""
	if request.Freshness != nil {
		freshness = *request.Freshness
	}
	var body []byte
	switch info.ChannelType {
	case constant.ChannelTypeTavily:
		baseURL.Path += "/search"
		depth := "basic"
		if request.Model == "tavily-search-advanced" {
			depth = "advanced"
		}
		params := map[string]any{
			"query": request.Query, "max_results": *request.MaxResults, "search_depth": depth,
			"topic": "general", "auto_parameters": false, "include_answer": false, "include_raw_content": false,
		}
		if freshness != "" {
			params["time_range"] = freshness
		}
		body, err = common.Marshal(params)
	case constant.ChannelTypeBraveSearch:
		method = http.MethodGet
		baseURL.Path += "/res/v1/web/search"
		params := url.Values{"q": {request.Query}, "count": {strconv.Itoa(*request.MaxResults)}, "result_filter": {"web"}}
		if freshness != "" {
			params.Set("freshness", map[string]string{"day": "pd", "week": "pw", "month": "pm", "year": "py"}[freshness])
		}
		baseURL.RawQuery = params.Encode()
	case constant.ChannelTypeBochaSearch:
		baseURL.Path += "/v1/web-search"
		bochaFreshness := map[string]string{"": "noLimit", "day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}[freshness]
		body, err = common.Marshal(map[string]any{"query": request.Query, "count": *request.MaxResults, "freshness": bochaFreshness, "summary": false})
	case constant.ChannelTypeBaiduSearch:
		if freshness == "day" {
			return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgSearchUnsupportedFreshness, map[string]any{"Model": request.Model, "Freshness": freshness})), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
		}
		baseURL.Path += "/v2/ai_search/web_search"
		params := map[string]any{
			"messages":             []map[string]string{{"role": "user", "content": request.Query}},
			"search_source":        "baidu_search_v2",
			"edition":              "standard",
			"resource_type_filter": []map[string]any{{"type": "web", "top_k": *request.MaxResults}},
		}
		if freshness != "" {
			params["search_recency_filter"] = freshness
		}
		body, err = common.Marshal(params)
	}
	if err != nil {
		return nil, types.NewError(err, types.ErrorCodeConvertRequestFailed, types.ErrOptionWithSkipRetry())
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 60*time.Second)
	defer cancel()
	upstream, err := http.NewRequestWithContext(ctx, method, baseURL.String(), bytes.NewReader(body))
	if err != nil {
		return nil, types.NewError(errors.New(i18n.T(c, i18n.MsgSearchInvalidBaseURL)), types.ErrorCodeInvalidRequest, types.ErrOptionWithSkipRetry())
	}
	upstream.Header.Set("Content-Type", "application/json")
	upstream.Header.Set("Accept", "application/json")
	if err := channel.ApplyHeaderOverride(info, c, upstream); err != nil {
		return nil, types.NewError(errors.New(i18n.T(c, i18n.MsgSearchChannelOverrides)), types.ErrorCodeInvalidRequest, types.ErrOptionWithSkipRetry())
	}
	// Never send the caller's relay key or a second provider's credential.
	upstream.Header.Del("Authorization")
	upstream.Header.Del("X-Subscription-Token")
	upstream.Header.Del("mj-api-secret")
	if info.ChannelType == constant.ChannelTypeBraveSearch {
		upstream.Header.Set("X-Subscription-Token", info.ApiKey)
	} else {
		upstream.Header.Set("Authorization", "Bearer "+info.ApiKey)
	}
	resp, err := channel.DoRequest(c, upstream, info)
	if err != nil {
		return nil, types.NewOpenAIError(errors.New(i18n.T(c, i18n.MsgSearchUpstreamFailed)), types.ErrorCodeDoRequestFailed, http.StatusBadGateway)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		status := resp.StatusCode
		if status < 400 {
			status = http.StatusBadGateway
		}
		return nil, types.NewOpenAIError(errors.New(i18n.T(c, i18n.MsgSearchUpstreamFailed)), types.ErrorCodeBadResponseStatusCode, status)
	}
	info.SetFirstResponseTime()
	const maxSearchResponseBytes = 8 << 20
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxSearchResponseBytes+1))
	if err != nil || len(raw) > maxSearchResponseBytes || !gjson.ValidBytes(raw) {
		return nil, types.NewOpenAIError(errors.New(i18n.T(c, i18n.MsgSearchInvalidResponse)), types.ErrorCodeBadResponseBody, http.StatusBadGateway)
	}
	results, valid := normalizeSearchResults(info.ChannelType, raw)
	if !valid {
		return nil, types.NewOpenAIError(errors.New(i18n.T(c, i18n.MsgSearchInvalidResponse)), types.ErrorCodeBadResponseBody, http.StatusBadGateway)
	}
	if len(results) > *request.MaxResults {
		results = results[:*request.MaxResults]
	}
	return &dto.SearchResponse{ID: c.GetString(common.RequestIdKey), Model: info.OriginModelName, Query: request.Query, Results: results}, nil
}

func normalizeSearchResults(channelType int, raw []byte) ([]dto.SearchResult, bool) {
	var items gjson.Result
	var stringFields []string
	switch channelType {
	case constant.ChannelTypeTavily:
		if gjson.GetBytes(raw, "error").Exists() {
			return nil, false
		}
		items = gjson.GetBytes(raw, "results")
		stringFields = []string{"title", "content", "published_date"}
	case constant.ChannelTypeBraveSearch:
		if gjson.GetBytes(raw, "error").Exists() {
			return nil, false
		}
		items = gjson.GetBytes(raw, "web.results")
		stringFields = []string{"title", "description", "page_age", "profile.name"}
		// Brave omits the web section when no web results are found.
		if !gjson.GetBytes(raw, "web").Exists() && gjson.GetBytes(raw, "query.original").Type == gjson.String {
			return []dto.SearchResult{}, true
		}
	case constant.ChannelTypeBochaSearch:
		if code := gjson.GetBytes(raw, "code"); code.Type != gjson.Number || code.Float() != 200 {
			return nil, false
		}
		// Bocha's official client treats an omitted webPages section as no
		// results; a missing data object or malformed section is still an error.
		data := gjson.GetBytes(raw, "data")
		if data.IsObject() && !data.Get("webPages").Exists() {
			return []dto.SearchResult{}, true
		}
		items = gjson.GetBytes(raw, "data.webPages.value")
		stringFields = []string{"name", "snippet", "datePublished", "siteName"}
	case constant.ChannelTypeBaiduSearch:
		// Baidu reports logical errors with a code even when HTTP is 200.
		if gjson.GetBytes(raw, "code").Exists() {
			return nil, false
		}
		for _, field := range []string{"error", "message"} {
			if value := gjson.GetBytes(raw, field); value.Exists() && value.String() != "" {
				return nil, false
			}
		}
		if requestID := gjson.GetBytes(raw, "request_id"); requestID.Type != gjson.String || strings.TrimSpace(requestID.String()) == "" {
			return nil, false
		}
		items = gjson.GetBytes(raw, "references")
		// The success schema requires request_id but makes references optional.
		// An omitted array is empty; an explicit null is still malformed.
		if !items.Exists() {
			return []dto.SearchResult{}, true
		}
		stringFields = []string{"title", "snippet", "content", "date", "website", "type"}
	}
	if !items.IsArray() {
		return nil, false
	}
	results := make([]dto.SearchResult, 0, len(items.Array()))
	for _, item := range items.Array() {
		if !item.IsObject() || item.Get("url").Type != gjson.String || item.Get("url").String() == "" {
			return nil, false
		}
		for _, field := range stringFields {
			if value := item.Get(field); value.Exists() && value.Type != gjson.String {
				return nil, false
			}
		}
		result := dto.SearchResult{URL: item.Get("url").String()}
		switch channelType {
		case constant.ChannelTypeTavily:
			result.Title, result.Content = item.Get("title").String(), item.Get("content").String()
			result.PublishedDate = item.Get("published_date").String()
		case constant.ChannelTypeBraveSearch:
			result.Title, result.Content = item.Get("title").String(), item.Get("description").String()
			result.PublishedDate, result.SiteName = item.Get("page_age").String(), item.Get("profile.name").String()
		case constant.ChannelTypeBochaSearch:
			result.Title, result.Content = item.Get("name").String(), item.Get("snippet").String()
			result.PublishedDate, result.SiteName = item.Get("datePublished").String(), item.Get("siteName").String()
		case constant.ChannelTypeBaiduSearch:
			if aladdin := item.Get("is_aladdin"); aladdin.Exists() {
				if aladdin.Type != gjson.True && aladdin.Type != gjson.False {
					return nil, false
				}
				if aladdin.Bool() {
					continue
				}
			}
			if resourceType := item.Get("type"); resourceType.Exists() && resourceType.String() != "web" {
				continue
			}
			result.Title, result.Content = item.Get("title").String(), item.Get("snippet").String()
			if result.Content == "" {
				result.Content = item.Get("content").String()
			}
			result.PublishedDate, result.SiteName = item.Get("date").String(), item.Get("website").String()
		}
		results = append(results, result)
	}
	return results, true
}
