package model

import (
	"testing"

	"github.com/QuantumNous/new-api/common"
	"github.com/glebarez/sqlite"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"gorm.io/gorm"
)

func TestTokenAutoGroupsRoundTripThroughRedisHashCache(t *testing.T) {
	useUserCacheMiniRedis(t)
	token := Token{
		Id:         42,
		UserId:     7,
		Key:        "token-auto-groups-cache-key",
		Name:       "auto-cache",
		Group:      "auto",
		AutoGroups: `["vip","default"]`,
	}

	require.NoError(t, cacheSetTokenForTest(token))
	cached, err := cacheGetTokenByKey(token.Key)
	require.NoError(t, err)
	assert.Equal(t, token.AutoGroups, cached.AutoGroups)
	groups, err := cached.GetAutoGroups()
	require.NoError(t, err)
	assert.Equal(t, []string{"vip", "default"}, groups)
}

func TestTokenAllowUserQuotaQueryRoundTripsThroughRedisHashCache(t *testing.T) {
	server := useUserCacheMiniRedis(t)
	token := Token{Id: 43, UserId: 7, Key: "token-user-quota-cache-key", Name: "user-quota-cache", AllowUserQuotaQuery: true}

	initialized, err := cacheInitToken(token)
	require.NoError(t, err)
	require.Equal(t, 1, initialized)
	cached, err := cacheGetTokenByKey(token.Key)
	require.NoError(t, err)
	assert.True(t, cached.AllowUserQuotaQuery)
	assert.Positive(t, server.TTL(getTokenCacheKey(token.Key)))
}

func TestTokenUpdateSynchronouslyNarrowsPreheatedAutoGroupsCache(t *testing.T) {
	truncateTables(t)
	useUserCacheMiniRedis(t)
	token := Token{
		UserId:          7,
		Key:             "token-auto-groups-update-cache-key",
		Name:            "auto-cache-update",
		Status:          common.TokenStatusEnabled,
		ExpiredTime:     -1,
		UnlimitedQuota:  true,
		Group:           "auto",
		CrossGroupRetry: true,
		AutoGroups:      `["default","vip"]`,
	}
	require.NoError(t, token.Insert())
	require.NoError(t, cacheSetTokenForTest(token))

	preheated, err := cacheGetTokenByKey(token.Key)
	require.NoError(t, err)
	assert.JSONEq(t, `["default","vip"]`, preheated.AutoGroups)

	require.NoError(t, token.SetAutoGroups([]string{"vip"}))
	require.NoError(t, token.Update())
	// Update 是限制性变更：写库前删除缓存并设置 fence。缓存不再提供旧的
	// 宽分组值，下一次读取必须看到收紧后的分组。
	_, cacheErr := cacheGetTokenByKey(token.Key)
	require.Error(t, cacheErr, "the pre-update cache entry must be invalidated")
	reloaded, err := GetTokenByKey(token.Key, false)
	require.NoError(t, err)
	assert.JSONEq(t, `["vip"]`, reloaded.AutoGroups)
}

// cacheSetTokenForTest 以测试身份写入完整 token 缓存（含额度字段），
// 模拟“已水合”的缓存状态。
func cacheSetTokenForTest(token Token) error {
	_, err := cacheInitToken(token)
	return err
}

func TestTokenQuotaGrantImmediatelyRestoresCachedExhaustedToken(t *testing.T) {
	oldDB := DB
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	require.NoError(t, err)
	DB = db
	t.Cleanup(func() { DB = oldDB; sqlDB, err := db.DB(); require.NoError(t, err); require.NoError(t, sqlDB.Close()) })
	require.NoError(t, db.AutoMigrate(&Token{}, &TokenQuotaGrant{}))
	server := useUserCacheMiniRedis(t)
	token := Token{UserId: 7, Key: "exhausted-grant-cache", Status: common.TokenStatusExhausted, ExpiredTime: -1, UsedQuota: 80}
	require.NoError(t, token.Insert())
	require.NoError(t, cacheSetTokenForTest(token))
	_, err = ValidateUserToken(token.Key)
	require.ErrorIs(t, err, ErrTokenQuotaExhausted)
	grant := TokenQuotaGrant{GrantID: "restore-cache", TokenID: token.Id, UserID: token.UserId, Quota: 25, Source: "beta"}
	require.NoError(t, db.Transaction(func(tx *gorm.DB) error { _, err := GrantTokenQuota(tx, grant); return err }))
	restored, err := ValidateUserToken(token.Key)
	require.NoError(t, err)
	assert.Equal(t, 25, restored.RemainQuota)
	assert.Equal(t, 80, restored.UsedQuota)
	assert.Equal(t, common.TokenStatusEnabled, restored.Status)
	// A pre-mutation reader cannot publish its exhausted snapshot after commit.
	initialized, err := cacheInitToken(token)
	require.NoError(t, err)
	assert.Zero(t, initialized)
	grant.GrantID = "redis-unavailable"
	server.SetError("redis unavailable")
	require.Error(t, db.Transaction(func(tx *gorm.DB) error { _, err := GrantTokenQuota(tx, grant); return err }))
	server.SetError("")
	var count int64
	require.NoError(t, db.Model(&TokenQuotaGrant{}).Where("grant_id = ?", grant.GrantID).Count(&count).Error)
	assert.Zero(t, count)
	require.NoError(t, db.First(&restored, token.Id).Error)
	assert.Equal(t, 25, restored.RemainQuota)
}
