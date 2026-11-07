package model

import (
	"errors"
	"regexp"

	"github.com/QuantumNous/new-api/common"
	"github.com/shopspring/decimal"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

type TokenQuotaGrant struct {
	GrantID   string `json:"grant_id" gorm:"primaryKey;type:varchar(64)"`
	TokenID   int    `json:"-" gorm:"not null;index:idx_token_quota_grants_token_created_at,priority:1"`
	UserID    int    `json:"-" gorm:"not null"`
	Quota     int    `json:"quota" gorm:"not null"`
	Source    string `json:"source" gorm:"type:varchar(16);not null"`
	CreatedAt int64  `json:"created_at" gorm:"not null;index:idx_token_quota_grants_token_created_at,priority:2;autoCreateTime:false"`
}

var tokenQuotaGrantID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

var (
	ErrInvalidTokenQuotaGrant  = common.NewMessage("Invalid token quota grant")
	ErrTokenQuotaGrantConflict = common.NewMessage("Token quota grant conflicts with an existing grant")
	ErrTokenQuotaLimitExceeded = common.NewMessage("Token quota limit exceeded")
)

// MaxTokenQuota is shared by absolute token edits and additive grants.
func MaxTokenQuota() int {
	quota, err := common.WalletQuotaFromDecimalStrict(
		decimal.NewFromInt(1_000_000_000).Mul(decimal.NewFromFloat(common.QuotaPerUnit)),
	)
	if err != nil {
		return common.MaxWalletQuota
	}
	return quota
}

// GrantTokenQuota credits only the token, never its used quota or the user's
// wallet. The caller owns the transaction and must roll it back on any error;
// payment settlement can therefore credit the wallet and token atomically.
func GrantTokenQuota(tx *gorm.DB, grant TokenQuotaGrant) (*Token, error) {
	if tx == nil || !tokenQuotaGrantID.MatchString(grant.GrantID) || grant.TokenID <= 0 || grant.UserID <= 0 ||
		grant.Quota <= 0 || grant.Quota > MaxTokenQuota() || grant.Source == "" || len(grant.Source) > 16 {
		return nil, ErrInvalidTokenQuotaGrant
	}
	var token Token
	if err := lockForUpdate(tx).Where("id = ? AND user_id = ?", grant.TokenID, grant.UserID).First(&token).Error; err != nil {
		return nil, err
	}
	var existing TokenQuotaGrant
	err := tx.Where("grant_id = ?", grant.GrantID).First(&existing).Error
	if err == nil {
		if existing.TokenID != grant.TokenID || existing.Quota != grant.Quota || existing.Source != grant.Source || existing.UserID != grant.UserID {
			return nil, ErrTokenQuotaGrantConflict
		}
		return &token, nil
	}
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, err
	}
	// Check before adding to avoid integer overflow, and repeat the ceiling in
	// the UPDATE predicate so concurrent consumption cannot be overwritten.
	maxCurrent := MaxTokenQuota() - grant.Quota
	if token.RemainQuota > maxCurrent {
		return nil, ErrTokenQuotaLimitExceeded
	}
	if err := invalidateTokenCacheForMutation(token.Key); err != nil {
		return nil, err
	}
	grant.CreatedAt = common.GetTimestamp()
	inserted := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&grant)
	if inserted.Error != nil {
		return nil, inserted.Error
	}
	if inserted.RowsAffected == 0 {
		// Another token may have claimed this globally unique grant ID while we
		// waited. Never credit a token unless this transaction inserted the grant.
		return nil, ErrTokenQuotaGrantConflict
	}
	updated := tx.Model(&Token{}).Where("id = ? AND user_id = ? AND remain_quota <= ?", token.Id, grant.UserID, maxCurrent).
		Updates(map[string]any{
			"remain_quota": gorm.Expr("remain_quota + ?", grant.Quota),
			"status":       gorm.Expr("CASE WHEN status = ? THEN ? ELSE status END", common.TokenStatusExhausted, common.TokenStatusEnabled),
		})
	if updated.Error != nil {
		return nil, updated.Error
	}
	if updated.RowsAffected != 1 {
		return nil, ErrTokenQuotaLimitExceeded
	}
	if err := tx.First(&token, token.Id).Error; err != nil {
		return nil, err
	}
	return &token, nil
}

func GetTokenQuotaGrants(tokenID, userID, offset, limit int) ([]TokenQuotaGrant, int64, error) {
	if _, err := GetTokenByIds(tokenID, userID); err != nil {
		return nil, 0, err
	}
	query := DB.Model(&TokenQuotaGrant{}).Where("token_id = ? AND user_id = ?", tokenID, userID)
	var total int64
	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	items := make([]TokenQuotaGrant, 0)
	err := query.Order("created_at DESC, grant_id DESC").Offset(offset).Limit(limit).Find(&items).Error
	return items, total, err
}
