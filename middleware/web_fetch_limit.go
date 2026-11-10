package middleware

import (
	"net/http"
	"strconv"
	"sync"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/logger"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/gin-gonic/gin"
)

const (
	webFetchWindowSeconds = 60
	webFetchWindowLimit   = 30
	webFetchTokenSlots    = 5
	webFetchGlobalSlots   = 20
)

var webFetchConcurrency = struct {
	sync.Mutex
	byToken map[int]int
	total   int
}{byToken: make(map[int]int)}

func WebFetchLimit() gin.HandlerFunc {
	return func(c *gin.Context) {
		tokenID := c.GetInt("token_id")
		webFetchConcurrency.Lock()
		var messageKey string
		switch {
		case webFetchConcurrency.byToken[tokenID] >= webFetchTokenSlots:
			messageKey = i18n.MsgWebFetchRateLimited
		case webFetchConcurrency.total >= webFetchGlobalSlots:
			messageKey = i18n.MsgWebFetchBusy
		default:
			webFetchConcurrency.byToken[tokenID]++
			webFetchConcurrency.total++
		}
		webFetchConcurrency.Unlock()
		if messageKey != "" {
			abortWithOpenAiMessage(c, http.StatusTooManyRequests, i18n.T(c, messageKey), types.ErrorCode("rate_limit_exceeded"))
			return
		}
		defer func() {
			webFetchConcurrency.Lock()
			webFetchConcurrency.total--
			webFetchConcurrency.byToken[tokenID]--
			if webFetchConcurrency.byToken[tokenID] == 0 {
				delete(webFetchConcurrency.byToken, tokenID)
			}
			webFetchConcurrency.Unlock()
		}()

		key := "WF:token:" + strconv.Itoa(tokenID)
		var allowed bool
		if common.RedisEnabled {
			var err error
			allowed, _, _, err = redisFixedWindowTake(c.Request.Context(), key, webFetchWindowLimit, webFetchWindowSeconds)
			if err != nil {
				logger.LogError(c, common.LogText("failed to check the web fetch request limit: %s", err.Error()))
				abortWithOpenAiMessage(c, http.StatusInternalServerError, "rate_limit_check_failed")
				return
			}
		} else {
			inMemoryRateLimiter.Init(common.RateLimitKeyExpirationDuration)
			allowed = inMemoryRateLimiter.Request(key, webFetchWindowLimit, webFetchWindowSeconds)
		}
		if !allowed {
			abortWithOpenAiMessage(c, http.StatusTooManyRequests, i18n.T(c, i18n.MsgWebFetchRateLimited), types.ErrorCode("rate_limit_exceeded"))
			return
		}
		c.Next()
	}
}
