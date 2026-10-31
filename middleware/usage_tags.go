package middleware

import (
	"regexp"

	"github.com/QuantumNous/new-api/common"
	"github.com/gin-gonic/gin"
)

var usageMarkerPattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,64}$`)

// UsageTags keeps opaque client accounting markers in the gateway only.
// Removing them from the request also prevents explicit or wildcard channel
// header passthrough from exposing these identifiers to upstream providers.
func UsageTags() gin.HandlerFunc {
	return func(c *gin.Context) {
		for _, name := range []string{common.UsageTagKey, common.UsageRefKey} {
			value := c.GetHeader(name)
			c.Request.Header.Del(name)
			if usageMarkerPattern.MatchString(value) {
				c.Set(name, value)
			}
		}
		c.Next()
	}
}
