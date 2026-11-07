package controller

import (
	"strconv"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/model"
	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

func GrantTokenQuota(c *gin.Context) {
	tokenID, err := strconv.Atoi(c.Param("id"))
	if err != nil || tokenID <= 0 {
		common.ApiErrorT(c, "Invalid parameters")
		return
	}
	var request struct {
		GrantID string `json:"grant_id"`
		Quota   int    `json:"quota"`
		Source  string `json:"source"`
	}
	if err := c.ShouldBindJSON(&request); err != nil {
		common.ApiErrorT(c, "Invalid parameters")
		return
	}
	if request.Source != "beta" {
		common.ApiError(c, model.ErrInvalidTokenQuotaGrant)
		return
	}
	grant := model.TokenQuotaGrant{GrantID: request.GrantID, TokenID: tokenID, UserID: c.GetInt("id"), Quota: request.Quota, Source: request.Source}
	var token *model.Token
	if err := model.DB.Transaction(func(tx *gorm.DB) error {
		var err error
		token, err = model.GrantTokenQuota(tx, grant)
		return err
	}); err != nil {
		common.ApiError(c, err)
		return
	}
	params := tokenAuditParams(c)
	params["grant_id"], params["quota"], params["source"] = grant.GrantID, grant.Quota, grant.Source
	common.SetContextKey(c, constant.ContextKeyTokenAuditSucceeded, true)
	common.ApiSuccess(c, buildMaskedTokenResponse(token))
}

func GetTokenQuotaGrants(c *gin.Context) {
	tokenID, err := strconv.Atoi(c.Param("id"))
	if err != nil || tokenID <= 0 {
		common.ApiErrorT(c, "Invalid parameters")
		return
	}
	page := common.GetPageQuery(c)
	// Reject negative/overflowed pagination instead of allowing GORM to omit
	// LIMIT and accidentally return every grant.
	if page.Page < 1 || page.PageSize < 1 || page.Page-1 > int(^uint(0)>>1)/page.PageSize {
		common.ApiErrorT(c, "Invalid parameters")
		return
	}
	items, total, err := model.GetTokenQuotaGrants(tokenID, c.GetInt("id"), page.GetStartIdx(), page.PageSize)
	if err != nil {
		common.ApiError(c, err)
		return
	}
	page.SetTotal(int(total))
	page.SetItems(items)
	common.ApiSuccess(c, page)
}
