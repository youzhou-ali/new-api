package helper

import (
	"encoding/json"
	"errors"
	"strings"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/gin-gonic/gin"
)

func GetAndValidateWebFetchRequest(c *gin.Context) (*dto.WebFetchRequest, error) {
	if c.ContentType() != gin.MIMEJSON {
		return nil, errors.New(i18n.T(c, i18n.MsgWebFetchJSONRequired))
	}

	var fields map[string]json.RawMessage
	if err := common.UnmarshalBodyReusable(c, &fields); err != nil {
		return nil, err
	}
	for field := range fields {
		if field != "model" && field != "url" {
			return nil, errors.New(i18n.T(c, i18n.MsgWebFetchUnsupportedField, map[string]any{"Field": field}))
		}
	}

	request := &dto.WebFetchRequest{}
	if err := common.UnmarshalBodyReusable(c, request); err != nil {
		return nil, err
	}
	if request.Model != constant.WebFetchModel {
		return nil, errors.New(i18n.T(c, i18n.MsgWebFetchInvalidModel))
	}
	if strings.TrimSpace(request.URL) == "" {
		return nil, errors.New(i18n.T(c, i18n.MsgWebFetchURLRequired))
	}
	return request, nil
}
