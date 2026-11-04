package helper

import (
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/relaykit/dto"
	"github.com/gin-gonic/gin"
)

func GetAndValidateSearchRequest(c *gin.Context) (*dto.SearchRequest, error) {
	if c.ContentType() != gin.MIMEJSON {
		return nil, errors.New(i18n.T(c, i18n.MsgSearchJSONRequired))
	}
	var fields map[string]json.RawMessage
	if err := common.UnmarshalBodyReusable(c, &fields); err != nil {
		return nil, err
	}
	for field := range fields {
		switch field {
		case "model", "query", "max_results", "freshness", "domains":
		default:
			return nil, errors.New(i18n.T(c, i18n.MsgSearchUnsupportedField, map[string]any{"Field": field}))
		}
	}
	request := &dto.SearchRequest{}
	if err := common.UnmarshalBodyReusable(c, request); err != nil {
		return nil, err
	}
	if constant.SearchChannelType(request.Model) == constant.ChannelTypeUnknown {
		return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidModel))
	}
	if strings.TrimSpace(request.Query) == "" || utf8.RuneCountInString(request.Query) > 400 || len(strings.Fields(request.Query)) > 50 {
		return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidQuery))
	}
	if request.MaxResults == nil {
		request.MaxResults = common.GetPointer(5)
	}
	if *request.MaxResults < 1 || *request.MaxResults > 20 {
		return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidCount))
	}
	if request.Freshness != nil {
		switch *request.Freshness {
		case "", "day", "week", "month", "year":
		default:
			return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidFreshness))
		}
		if request.Model == "baidu-search" && *request.Freshness == "day" {
			return nil, errors.New(i18n.T(c, i18n.MsgSearchUnsupportedFreshness, map[string]any{"Model": request.Model, "Freshness": *request.Freshness}))
		}
	}
	if len(request.Domains) > 20 {
		return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidDomains))
	}
	domains := make([]string, 0, len(request.Domains))
	for _, domain := range request.Domains {
		domain = strings.TrimSuffix(strings.ToLower(domain), ".")
		if len(domain) > 253 || !searchDomainPattern.MatchString(domain) {
			return nil, errors.New(i18n.T(c, i18n.MsgSearchInvalidDomains))
		}
		if !slices.Contains(domains, domain) {
			domains = append(domains, domain)
		}
	}
	request.Domains = domains
	return request, nil
}

// searchDomainPattern accepts a bare host name with at least two labels;
// internationalized names must already be punycode.
var searchDomainPattern = regexp.MustCompile(`^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
