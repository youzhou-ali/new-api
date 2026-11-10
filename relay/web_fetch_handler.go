package relay

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/logger"
	relaycommon "github.com/QuantumNous/new-api/relay/common"
	"github.com/QuantumNous/new-api/relay/helper"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/QuantumNous/new-api/relaykit/types"
	"github.com/QuantumNous/new-api/service"
	"github.com/gin-gonic/gin"
)

func WebFetchHelper(c *gin.Context, info *relaycommon.RelayInfo) *types.NewAPIError {
	response, apiErr := PerformWebFetch(c, info)
	if apiErr != nil {
		return apiErr
	}
	service.PostTextConsumeQuota(c, info, &dto.Usage{}, nil)
	c.JSON(http.StatusOK, response)
	return nil
}

// PerformWebFetch is shared by the relay and the channel test.
func PerformWebFetch(c *gin.Context, info *relaycommon.RelayInfo) (*dto.WebFetchResponse, *types.NewAPIError) {
	info.InitChannelMeta(c)
	request, ok := info.Request.(*dto.WebFetchRequest)
	if !ok || request.Model != constant.WebFetchModel || request.Model != info.OriginModelName || info.ChannelType != constant.ChannelTypeWebFetch {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgWebFetchInvalidChannel)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}
	if err := helper.ModelMappedHelper(c, info, nil); err != nil || info.UpstreamModelName != info.OriginModelName ||
		len(info.ParamOverride) != 0 || info.ChannelSetting.PassThroughBodyEnabled || strings.TrimSpace(info.ChannelSetting.Proxy) != "" {
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, i18n.MsgWebFetchChannelOverrides)), types.ErrorCodeInvalidRequest, http.StatusBadRequest, types.ErrOptionWithSkipRetry())
	}

	started := time.Now()
	ctx, cancel := context.WithTimeout(c.Request.Context(), service.WebFetchTimeout)
	defer cancel()
	result, fetchErr := service.WebFetch(ctx, request.URL)
	ms := time.Since(started).Milliseconds()
	if fetchErr != nil {
		logger.LogWarn(c, common.LogText("web fetch failed: code=%s host=%s path=%s ms=%d detail=%s",
			fetchErr.Code, fetchErr.Host, fetchErr.Path, ms, fetchErr.Detail))
		return nil, types.NewErrorWithStatusCode(errors.New(i18n.T(c, fetchErr.MessageKey)), fetchErr.Code, fetchErr.StatusCode, types.ErrOptionWithSkipRetry())
	}

	info.SetFirstResponseTime()
	info.WebFetchLog = map[string]any{
		"host": result.Host, "path": result.Path, "status": result.Status,
		"bytes": len(result.Body), "ms": ms,
	}
	return &dto.WebFetchResponse{
		URL: result.URL, Status: result.Status, ContentType: result.ContentType, Body: result.Body,
	}, nil
}
