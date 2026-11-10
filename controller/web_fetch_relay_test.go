package controller

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	hostdto "github.com/QuantumNous/new-api/dto"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/middleware"
	"github.com/QuantumNous/new-api/model"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/QuantumNous/new-api/service"
	"github.com/QuantumNous/new-api/setting/config"
	"github.com/QuantumNous/new-api/setting/operation_setting"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestWebFetchResponses(t *testing.T) {
	t.Cleanup(service.AllowWebFetchLoopbackForTest())
	const html = "<html><title>Web fetch</title></html>"
	oversized := bytes.Repeat([]byte("a"), (10<<20)+1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/html":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = io.WriteString(w, html)
		case "/gzip", "/large-gzip":
			assert.Equal(t, "gzip", r.Header.Get("Accept-Encoding"))
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Content-Encoding", "gzip")
			gz := gzip.NewWriter(w)
			if r.URL.Path == "/gzip" {
				_, _ = io.WriteString(gz, html)
			} else {
				_, _ = gz.Write(oversized)
			}
			_ = gz.Close()
		case "/large":
			_, _ = w.Write(oversized)
		case "/br":
			w.Header().Set("Content-Encoding", "br")
			_, _ = io.WriteString(w, "compressed content")
		case "/empty":
			w.Header().Set("Content-Encoding", "br")
			w.WriteHeader(http.StatusNoContent)
		case "/redirect-without-location":
			w.WriteHeader(http.StatusFound)
		case "/unfollowed-redirect":
			w.Header().Set("Location", "/html")
			w.WriteHeader(http.StatusMultipleChoices)
		default:
			hops, err := strconv.Atoi(strings.TrimPrefix(r.URL.Path, "/redirect/"))
			if err != nil || hops < 1 {
				http.NotFound(w, r)
				return
			}
			target := "/html"
			if hops > 1 {
				target = fmt.Sprintf("/redirect/%d", hops-1)
			}
			http.Redirect(w, r, target, http.StatusFound)
		}
	}))
	t.Cleanup(server.Close)

	for _, tc := range []struct {
		path, finalPath, body, contentType string
		status                             int
	}{
		{"/html", "/html", html, "text/html; charset=utf-8", http.StatusOK},
		{"/gzip", "/gzip", html, "text/html; charset=utf-8", http.StatusOK},
		{"/redirect/3", "/html", html, "text/html; charset=utf-8", http.StatusOK},
		{"/redirect/5", "/html", html, "text/html; charset=utf-8", http.StatusOK},
		{"/empty", "/empty", "", "", http.StatusNoContent},
		{"/redirect-without-location", "/redirect-without-location", "", "", http.StatusFound},
		{"/unfollowed-redirect", "/unfollowed-redirect", "", "", http.StatusMultipleChoices},
	} {
		t.Run(tc.path, func(t *testing.T) {
			result, err := service.WebFetch(t.Context(), server.URL+tc.path)
			require.Nil(t, err)
			require.NotNil(t, result)
			assert.Equal(t, server.URL+tc.finalPath, result.URL)
			assert.Equal(t, tc.status, result.Status)
			assert.Equal(t, tc.body, string(result.Body))
			assert.NotNil(t, result.Body)
			assert.Equal(t, tc.contentType, result.ContentType)
		})
	}

	for _, tc := range []struct {
		path, code string
	}{
		{"/redirect/6", "too_many_redirects"},
		{"/large", "too_large"},
		{"/large-gzip", "too_large"},
		{"/br", "unsupported_encoding"},
	} {
		t.Run(tc.path, func(t *testing.T) {
			result, err := service.WebFetch(t.Context(), server.URL+tc.path)
			require.NotNil(t, err)
			assert.Nil(t, result)
			assert.Equal(t, types.ErrorCode(tc.code), err.Code)
			assert.Equal(t, http.StatusBadGateway, err.StatusCode)
		})
	}
}

func TestWebFetchRejectsUnsafeTargets(t *testing.T) {
	for _, tc := range []struct {
		url, code string
		status    int
	}{
		{"http://127.0.0.1/", "blocked_address", http.StatusBadRequest},
		{"http://10.0.0.1/", "blocked_address", http.StatusBadRequest},
		{"http://169.254.169.254/", "blocked_address", http.StatusBadRequest},
		{"http://[::1]/", "blocked_address", http.StatusBadRequest},
		{"http://[::ffff:127.0.0.1]/", "blocked_address", http.StatusBadRequest},
		{"http://[2002:7f00:1::]/", "blocked_address", http.StatusBadRequest},
		{"http://[::7f00:1]/", "blocked_address", http.StatusBadRequest},
		{"http://[fec0::1]/", "blocked_address", http.StatusBadRequest},
		{"http://localhost/", "blocked_address", http.StatusBadRequest},
		{"ftp://example.com/", "invalid_url", http.StatusBadRequest},
		{"https://user:password@example.com/", "invalid_url", http.StatusBadRequest},
		{"http://example.com:8080/", "invalid_url", http.StatusBadRequest},
		{"http://example.com/" + strings.Repeat("x", 4000), "invalid_url", http.StatusBadRequest},
	} {
		t.Run(tc.url[:min(len(tc.url), 60)], func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			result, err := service.WebFetch(ctx, tc.url)
			require.NotNil(t, err)
			assert.Nil(t, result)
			assert.Equal(t, types.ErrorCode(tc.code), err.Code)
			assert.Equal(t, tc.status, err.StatusCode)
		})
	}
}

func TestWebFetchChecksEachRedirect(t *testing.T) {
	t.Cleanup(service.AllowWebFetchLoopbackForTest())
	for _, tc := range []struct {
		location, code string
	}{
		{"http://10.0.0.1/", "blocked_address"},
		{"http://169.254.169.254/", "blocked_address"},
		{"ftp://example.com/", "invalid_url"},
		{"http://example.com/%zz?token=redirect-secret", "invalid_url"},
	} {
		t.Run(tc.location, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Location", tc.location)
				w.WriteHeader(http.StatusTemporaryRedirect)
			}))
			defer server.Close()
			result, err := service.WebFetch(t.Context(), server.URL)
			require.NotNil(t, err)
			assert.Nil(t, result)
			assert.Equal(t, types.ErrorCode(tc.code), err.Code)
			assert.Equal(t, http.StatusBadRequest, err.StatusCode)
			assert.NotContains(t, err.Detail, "redirect-secret")
		})
	}
}

func TestWebFetchClassifiesConnectionFailures(t *testing.T) {
	t.Cleanup(service.AllowWebFetchLoopbackForTest())
	const querySecret = "web-fetch-query-secret"

	t.Run("DNS lookup failed", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		result, err := service.WebFetch(ctx, "http://nonexistent.invalid/")
		require.NotNil(t, err)
		assert.Nil(t, result)
		assert.Equal(t, types.ErrorCode("dns_failed"), err.Code)
		assert.Equal(t, http.StatusBadGateway, err.StatusCode)
	})
	t.Run("body deadline", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}))
		defer server.Close()
		ctx, cancel := context.WithTimeout(t.Context(), 50*time.Millisecond)
		defer cancel()
		_, err := service.WebFetch(ctx, server.URL)
		require.NotNil(t, err)
		assert.Equal(t, types.ErrorCode("timeout"), err.Code)
		assert.Equal(t, http.StatusGatewayTimeout, err.StatusCode)
	})
	t.Run("invalid certificate despite global TLS bypass", func(t *testing.T) {
		previous := common.TLSInsecureSkipVerify
		common.TLSInsecureSkipVerify = true
		t.Cleanup(func() { common.TLSInsecureSkipVerify = previous })
		server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Error("fetch must verify the server certificate")
		}))
		defer server.Close()
		_, err := service.WebFetch(t.Context(), server.URL+"/?token="+querySecret)
		require.NotNil(t, err)
		assert.Equal(t, types.ErrorCode("connect_failed"), err.Code)
		assert.Equal(t, http.StatusBadGateway, err.StatusCode)
		assert.Contains(t, err.Detail, "certificate")
		assert.NotContains(t, err.Detail, querySecret)
	})
	t.Run("closed server", func(t *testing.T) {
		server := httptest.NewServer(http.NotFoundHandler())
		server.Close()
		_, err := service.WebFetch(t.Context(), server.URL+"/?token="+querySecret)
		require.NotNil(t, err)
		assert.Equal(t, types.ErrorCode("connect_failed"), err.Code)
		assert.Equal(t, http.StatusBadGateway, err.StatusCode)
		assert.Contains(t, err.Detail, "connection refused")
		assert.NotContains(t, err.Detail, querySecret)
	})
}

const webFetchTestQuota = 100000

func setupWebFetchRelay(t *testing.T) (*gin.Engine, *model.User, *model.Token) {
	t.Helper()
	t.Cleanup(service.AllowWebFetchLoopbackForTest())
	user, token := setupResponsesWSRequestTest(t)
	require.NoError(t, i18n.Init())
	previousConfig := config.GlobalConfig.ExportAllConfigs()
	previousBatch, previousLogs, previousRetries := common.BatchUpdateEnabled, common.LogConsumeEnabled, common.RetryTimes
	common.BatchUpdateEnabled, common.LogConsumeEnabled, common.RetryTimes = false, true, 1
	t.Cleanup(func() {
		require.NoError(t, config.GlobalConfig.LoadFromDB(previousConfig))
		common.BatchUpdateEnabled, common.LogConsumeEnabled, common.RetryTimes = previousBatch, previousLogs, previousRetries
	})
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{
		"billing_setting.billing_mode":    "{}",
		"billing_setting.billing_expr":    "{}",
		"group_ratio_setting.group_ratio": `{"default":1}`,
		"quota_setting.trust_quota_usd":   "0",
		"perf_metrics_setting.enabled":    "false",
	}))
	require.NoError(t, model.DB.AutoMigrate(&model.Channel{}, &model.Ability{}))
	require.NoError(t, model.LOG_DB.AutoMigrate(&model.Log{}))
	require.NoError(t, model.DB.Model(user).Updates(map[string]any{
		"quota": webFetchTestQuota, "setting": `{"billing_preference":"wallet_only"}`,
	}).Error)
	// The in-memory rate window survives between fixtures, including -count runs.
	require.NoError(t, model.DB.Model(token).Updates(map[string]any{"id": user.Id, "remain_quota": webFetchTestQuota}).Error)
	token.Id = user.Id

	engine := gin.New()
	engine.Use(middleware.UsageTags(), middleware.BodyStorageCleanup())
	engine.Use(func(c *gin.Context) { c.Set(common.RequestIdKey, "web-fetch-test"); c.Next() })
	engine.POST("/v1/fetch", middleware.TokenAuth(), middleware.WebFetchLimit(), middleware.Distribute(), func(c *gin.Context) {
		Relay(c, types.RelayFormatWebFetch)
	})
	return engine, user, token
}

func addWebFetchRelayChannel(t *testing.T, channelType int, priority int64) *model.Channel {
	t.Helper()
	channel := &model.Channel{
		Name: t.Name(), Models: "web-fetch", Type: channelType, Group: "default", Key: "unused-channel-key",
		Status: common.ChannelStatusEnabled, AutoBan: common.GetPointer(1), Priority: common.GetPointer(priority),
		HeaderOverride: common.GetPointer(`{"X-Channel-Header":"private"}`),
	}
	require.NoError(t, model.DB.Create(channel).Error)
	require.NoError(t, channel.AddAbilities(nil))
	t.Cleanup(func() {
		require.NoError(t, model.DB.Unscoped().Where("channel_id = ?", channel.Id).Delete(&model.Ability{}).Error)
		require.NoError(t, model.DB.Unscoped().Delete(channel).Error)
	})
	return channel
}

func webFetchRelayRequest(ctx context.Context, key, body string) *http.Request {
	request := httptest.NewRequestWithContext(ctx, http.MethodPost, "/v1/fetch", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept-Language", "en")
	request.Header.Set("Authorization", "Bearer sk-"+key)
	return request
}

func serveWebFetchRelay(engine *gin.Engine, request *http.Request) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	engine.ServeHTTP(recorder, request)
	return recorder
}

func assertWebFetchRelayError(t *testing.T, response *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	require.Equal(t, status, response.Code, response.Body.String())
	var body struct {
		Error struct {
			Type string `json:"type"`
			Code string `json:"code"`
		} `json:"error"`
	}
	require.NoError(t, common.Unmarshal(response.Body.Bytes(), &body))
	assert.Equal(t, "new_api_error", body.Error.Type)
	assert.Equal(t, code, body.Error.Code)
}

func TestWebFetchRelayReturnsRawBytesAndRecordsFreeUsage(t *testing.T) {
	engine, user, token := setupWebFetchRelay(t)
	channel := addWebFetchRelayChannel(t, constant.ChannelTypeWebFetch, 10)
	addWebFetchRelayChannel(t, constant.ChannelTypeOpenAI, 100)
	png := []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n', 0, 0xff}
	var visits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		visits.Add(1)
		assert.Equal(t, http.MethodGet, r.Method)
		assert.Zero(t, r.ContentLength)
		for name := range r.Header {
			assert.Contains(t, []string{"User-Agent", "Accept", "Accept-Encoding", "Connection"}, name)
		}
		assert.Equal(t, "Mozilla/5.0 (compatible; CubeAgent/relay)", r.Header.Get("User-Agent"))
		assert.Equal(t, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", r.Header.Get("Accept"))
		assert.Equal(t, "gzip", r.Header.Get("Accept-Encoding"))
		switch r.URL.Path {
		case "/redirect":
			http.SetCookie(w, &http.Cookie{Name: "target-session", Value: "private"})
			http.Redirect(w, r, "/result%20image?token=target-secret", http.StatusFound)
		case "/result image":
			w.Header().Set("Content-Type", "image/png")
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write(png)
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	t.Cleanup(server.Close)
	request := webFetchRelayRequest(t.Context(), token.Key, fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL+"/redirect"))
	request.Header.Set("Cookie", "client-session=private")
	request.Header.Set("Referer", "https://client.example/private")
	request.Header.Set(common.UsageTagKey, "fetch")
	request.Header.Set(common.UsageRefKey, "task-123")
	response := serveWebFetchRelay(engine, request)
	require.Equal(t, http.StatusOK, response.Code, response.Body.String())
	var body struct {
		URL         string `json:"url"`
		Status      int    `json:"status"`
		ContentType string `json:"content_type"`
		Body        string `json:"body"`
	}
	require.NoError(t, common.Unmarshal(response.Body.Bytes(), &body))
	assert.Equal(t, server.URL+"/result%20image?token=target-secret", body.URL)
	assert.Equal(t, http.StatusNotFound, body.Status)
	assert.Equal(t, "image/png", body.ContentType)
	decoded, err := base64.StdEncoding.DecodeString(body.Body)
	require.NoError(t, err)
	assert.Equal(t, png, decoded)
	assert.EqualValues(t, 2, visits.Load())

	var log model.Log
	require.NoError(t, model.LOG_DB.Where("token_id = ? AND type = ?", token.Id, model.LogTypeConsume).First(&log).Error)
	assert.Equal(t, channel.Id, log.ChannelId)
	assert.Equal(t, "task-123", log.UsageRef)
	assert.Equal(t, "fetch", log.UsageTag)
	assert.Zero(t, log.Quota)
	assert.NotContains(t, strings.ToLower(log.Content), "no usage")
	assert.NotContains(t, log.Other, "target-secret")
	var other struct {
		BillingUnit string `json:"billing_unit"`
		WebFetch    struct {
			Host   string `json:"host"`
			Path   string `json:"path"`
			Status int    `json:"status"`
			Bytes  int    `json:"bytes"`
			MS     *int64 `json:"ms"`
		} `json:"web_fetch"`
	}
	require.NoError(t, common.UnmarshalJsonStr(log.Other, &other))
	assert.Equal(t, "request", other.BillingUnit)
	assert.Equal(t, "127.0.0.1", other.WebFetch.Host)
	assert.Equal(t, "/result%20image", other.WebFetch.Path)
	assert.Equal(t, http.StatusNotFound, other.WebFetch.Status)
	assert.Equal(t, len(png), other.WebFetch.Bytes)
	assert.NotNil(t, other.WebFetch.MS)
	require.NoError(t, model.DB.First(user, user.Id).Error)
	require.NoError(t, model.DB.First(token, token.Id).Error)
	assert.Equal(t, webFetchTestQuota, user.Quota)
	assert.Equal(t, webFetchTestQuota, token.RemainQuota)

	empty := serveWebFetchRelay(engine, webFetchRelayRequest(t.Context(), token.Key, fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL+"/empty")))
	require.Equal(t, http.StatusOK, empty.Code, empty.Body.String())
	assert.JSONEq(t, fmt.Sprintf(`{"url":%q,"status":204,"content_type":"","body":""}`, server.URL+"/empty"), empty.Body.String())
}

func TestWebFetchRelayRejectsInvalidRequests(t *testing.T) {
	engine, _, token := setupWebFetchRelay(t)
	addWebFetchRelayChannel(t, constant.ChannelTypeWebFetch, 0)
	var visits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		visits.Add(1)
	}))
	t.Cleanup(server.Close)
	validBody := fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL)
	unauthorized := serveWebFetchRelay(engine, webFetchRelayRequest(t.Context(), "invalid-key", validBody))
	assert.Equal(t, http.StatusUnauthorized, unauthorized.Code, unauthorized.Body.String())

	for _, tc := range []struct {
		name, body, contentType, code, message string
	}{
		{"model", `{"model":"brave-search","url":"https://example.com"}`, "application/json", "", "model must be web-fetch"},
		{"unknown field", fmt.Sprintf(`{"model":"web-fetch","url":%q,"headers":{}}`, server.URL), "application/json", "invalid_request", "Unsupported web fetch field"},
		{"empty URL", `{"model":"web-fetch","url":""}`, "application/json", "invalid_request", "url is required"},
		{"invalid URL", `{"model":"web-fetch","url":"ftp://example.com/"}`, "application/json", "invalid_url", "Invalid URL"},
		{"non JSON", validBody, "text/plain", "invalid_request", "Web fetch requires application/json"},
		{"malformed media type", validBody, "application/json garbage", "invalid_request", "Web fetch requires application/json"},
		{"case-insensitive media type", `{"model":"web-fetch","url":"ftp://example.com/"}`, "Application/JSON; charset=utf-8", "invalid_url", "Invalid URL"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request := webFetchRelayRequest(t.Context(), token.Key, tc.body)
			request.Header.Set("Content-Type", tc.contentType)
			response := serveWebFetchRelay(engine, request)
			assertWebFetchRelayError(t, response, http.StatusBadRequest, tc.code)
			assert.Contains(t, response.Body.String(), tc.message)
		})
	}

	require.NoError(t, model.DB.Model(token).Updates(map[string]any{"model_limits_enabled": true, "model_limits": "brave-search"}).Error)
	forbidden := serveWebFetchRelay(engine, webFetchRelayRequest(t.Context(), token.Key, validBody))
	assert.Equal(t, http.StatusForbidden, forbidden.Code, forbidden.Body.String())
	require.NoError(t, model.DB.Model(token).Update("model_limits_enabled", false).Error)
	require.NoError(t, config.GlobalConfig.LoadFromDB(map[string]string{
		"billing_setting.billing_mode": `{"web-fetch":"tiered_expr"}`,
		"billing_setting.billing_expr": `{"web-fetch":"tier(\"base\", p * 1)"}`,
	}))
	tokenPriced := serveWebFetchRelay(engine, webFetchRelayRequest(t.Context(), token.Key, validBody))
	assertWebFetchRelayError(t, tokenPriced, http.StatusBadRequest, "model_price_error")
	assert.Contains(t, tokenPriced.Body.String(), "web-fetch requires a fixed per-request price")
	assert.Zero(t, visits.Load())
}

func TestWebFetchRelayDoesNotRetryOrDisableChannelsOnTargetFailure(t *testing.T) {
	engine, _, token := setupWebFetchRelay(t)
	previousAutoDisable, previousRanges := common.AutomaticDisableChannelEnabled, operation_setting.AutomaticDisableStatusCodeRanges
	common.AutomaticDisableChannelEnabled = true
	require.NoError(t, operation_setting.AutomaticDisableStatusCodesFromString("502"))
	t.Cleanup(func() {
		common.AutomaticDisableChannelEnabled = previousAutoDisable
		operation_setting.AutomaticDisableStatusCodeRanges = previousRanges
	})
	first := addWebFetchRelayChannel(t, constant.ChannelTypeWebFetch, 10)
	second := addWebFetchRelayChannel(t, constant.ChannelTypeWebFetch, 5)
	var visits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		visits.Add(1)
		conn, _, err := w.(http.Hijacker).Hijack()
		if assert.NoError(t, err) {
			_ = conn.Close()
		}
	}))
	t.Cleanup(server.Close)
	response := serveWebFetchRelay(engine, webFetchRelayRequest(t.Context(), token.Key, fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL+"/reset?token=target-secret")))
	assertWebFetchRelayError(t, response, http.StatusBadGateway, "connect_failed")
	assert.NotContains(t, response.Body.String(), "target-secret")
	assert.EqualValues(t, 1, visits.Load())
	for _, channel := range []*model.Channel{first, second} {
		require.NoError(t, model.DB.First(channel, channel.Id).Error)
		assert.Equal(t, common.ChannelStatusEnabled, channel.Status)
	}
	var logs int64
	require.NoError(t, model.LOG_DB.Model(&model.Log{}).Where("token_id = ? AND type = ?", token.Id, model.LogTypeConsume).Count(&logs).Error)
	assert.Zero(t, logs)
}

func TestWebFetchRelayEnforcesPerKeyConcurrencyAndWindow(t *testing.T) {
	engine, _, token := setupWebFetchRelay(t)
	addWebFetchRelayChannel(t, constant.ChannelTypeWebFetch, 0)
	entered := make(chan struct{}, 5)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/hold" {
			select {
			case entered <- struct{}{}:
			default:
			}
			select {
			case <-release:
			case <-r.Context().Done():
			}
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(server.Close)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	holdBody := fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL+"/hold")
	readyBody := fmt.Sprintf(`{"model":"web-fetch","url":%q}`, server.URL+"/ready")
	var workers sync.WaitGroup
	responses := make(chan *httptest.ResponseRecorder, 5)
	unblock := sync.OnceFunc(func() { close(release) })
	defer func() {
		unblock()
		workers.Wait()
	}()
	for range 5 {
		workers.Go(func() {
			responses <- serveWebFetchRelay(engine, webFetchRelayRequest(ctx, token.Key, holdBody))
		})
	}
	for range 5 {
		select {
		case <-entered:
		case <-ctx.Done():
			t.Fatal("five concurrent requests did not reach the target")
		}
	}
	limited := serveWebFetchRelay(engine, webFetchRelayRequest(ctx, token.Key, readyBody))
	assertWebFetchRelayError(t, limited, http.StatusTooManyRequests, "rate_limit_exceeded")
	unblock()
	workers.Wait()
	for range 5 {
		response := <-responses
		require.Equal(t, http.StatusOK, response.Code, response.Body.String())
	}
	// Concurrency rejections do not consume the 30-request window.
	for range 25 {
		response := serveWebFetchRelay(engine, webFetchRelayRequest(ctx, token.Key, readyBody))
		require.Equal(t, http.StatusOK, response.Code, response.Body.String())
	}
	windowLimited := serveWebFetchRelay(engine, webFetchRelayRequest(ctx, token.Key, readyBody))
	assertWebFetchRelayError(t, windowLimited, http.StatusTooManyRequests, "rate_limit_exceeded")
}

func TestWebFetchChannelConstraints(t *testing.T) {
	channel := model.Channel{Type: constant.ChannelTypeWebFetch, Models: "web-fetch"}
	require.NoError(t, channel.ValidateSearchConfiguration())
	for _, tc := range []struct {
		path, model string
		channelType int
		allowed     bool
	}{
		{"/v1/fetch", "web-fetch", constant.ChannelTypeWebFetch, true},
		{"/v1/fetch", "web-fetch", constant.ChannelTypeOpenAI, false},
		{"/v1/fetch", "brave-search", constant.ChannelTypeBraveSearch, false},
		{"/v1/chat/completions", "web-fetch", constant.ChannelTypeWebFetch, false},
		{"/v1/search", "web-fetch", constant.ChannelTypeWebFetch, false},
	} {
		candidate := channel
		candidate.Type = tc.channelType
		allowed, _ := model.ChannelSatisfiesFilters(&candidate, tc.model, []hostdto.ChannelFilter{
			{Kind: hostdto.FilterRequestPath, RequestPath: tc.path},
		})
		assert.Equal(t, tc.allowed, allowed, "%s with channel type %d", tc.path, tc.channelType)
	}
	for _, tc := range []struct {
		name   string
		update model.Channel
	}{
		{"other model", model.Channel{Models: "web-fetch,brave-search"}},
		{"mapping", model.Channel{ModelMapping: common.GetPointer(`{"web-fetch":"brave-search"}`)}},
		{"override", model.Channel{ParamOverride: common.GetPointer(`{"url":"https://example.com"}`)}},
		{"passthrough", model.Channel{Setting: common.GetPointer(`{"pass_through_body_enabled":true}`)}},
		{"proxy", model.Channel{Setting: common.GetPointer(`{"proxy":"http://proxy.example:8080"}`)}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			assert.Error(t, channel.ValidateSearchUpdate(&tc.update))
			tc.update.Type = constant.ChannelTypeWebFetch
			if tc.update.Models == "" {
				tc.update.Models = "web-fetch"
			}
			assert.Error(t, tc.update.ValidateSearchConfiguration())
			allowed, _ := model.ChannelSatisfiesFilters(&tc.update, "web-fetch", []hostdto.ChannelFilter{
				{Kind: hostdto.FilterRequestPath, RequestPath: "/v1/fetch"},
			})
			assert.False(t, allowed)
		})
	}
}
