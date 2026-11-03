package controller

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/middleware"
	"github.com/QuantumNous/new-api/model"
	"github.com/QuantumNous/new-api/service"
	"github.com/QuantumNous/new-api/setting/operation_setting"

	"github.com/alicebob/miniredis/v2"
	"github.com/gin-gonic/gin"
	"github.com/go-redis/redis/v8"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"gorm.io/gorm"
)

// callLogHandler runs a log handler as the given user and returns the JSON
// lines of the response.
func callLogHandler(t *testing.T, handler gin.HandlerFunc, userId int, role int, query string) []map[string]any {
	t.Helper()
	recorder := httptest.NewRecorder()
	ctx, _ := gin.CreateTestContext(recorder)
	ctx.Request = httptest.NewRequest(http.MethodGet, "/api/log?"+query, nil)
	ctx.Set("id", userId)
	ctx.Set("role", role)
	handler(ctx)
	var lines []map[string]any
	for line := range strings.SplitSeq(strings.TrimSpace(recorder.Body.String()), "\n") {
		var value map[string]any
		require.NoError(t, common.UnmarshalJsonStr(line, &value))
		lines = append(lines, value)
	}
	return lines
}

func exportedRequestIds(lines []map[string]any) []any {
	ids := make([]any, 0, len(lines))
	for _, line := range lines[:len(lines)-1] {
		ids = append(ids, line["request_id"])
	}
	return ids
}

// seedExportLogs stores logs of two users: three in the last hour and one
// from 400 days ago.
func seedExportLogs(t *testing.T, mainDB *gorm.DB, logDB *gorm.DB, now int64) {
	t.Helper()
	require.NoError(t, mainDB.Create(&model.Channel{Id: 9, Name: "primary"}).Error)
	logs := []model.Log{
		{UserId: 1, Username: "alice", CreatedAt: now - 3600, Type: model.LogTypeConsume, ModelName: "gpt-a", ChannelId: 9, Quota: 10, RequestId: "a-1", Other: `{"admin_info":{"use_channel":["9"]}}`},
		{UserId: 2, Username: "bob", CreatedAt: now - 1800, Type: model.LogTypeConsume, ModelName: "gpt-b", ChannelId: 9, Quota: 20, RequestId: "b-1"},
		{UserId: 1, Username: "alice", CreatedAt: now - 60, Type: model.LogTypeError, ModelName: "gpt-a", RequestId: "a-2"},
		{UserId: 1, Username: "alice", CreatedAt: now - 400*24*3600, Type: model.LogTypeConsume, ModelName: "gpt-a", RequestId: "a-old"},
	}
	for i := range logs {
		require.NoError(t, logDB.Create(&logs[i]).Error)
	}
}

func useLogExportDatabases(t *testing.T, mainDB *gorm.DB, logDB *gorm.DB, mainType common.DatabaseType, logType common.DatabaseType) {
	t.Helper()
	previousDB, previousLogDB := model.DB, model.LOG_DB
	previousMain, previousLog := common.MainDatabaseType(), common.LogDatabaseType()
	previousRedis, previousMemory := common.RedisEnabled, common.MemoryCacheEnabled
	model.DB, model.LOG_DB = mainDB, logDB
	common.SetDatabaseTypes(mainType, logType)
	common.RedisEnabled, common.MemoryCacheEnabled = false, false
	t.Cleanup(func() {
		model.DB, model.LOG_DB = previousDB, previousLogDB
		common.SetDatabaseTypes(previousMain, previousLog)
		common.RedisEnabled, common.MemoryCacheEnabled = previousRedis, previousMemory
	})
}

func TestLogExportDatabaseMatrix(t *testing.T) {
	gin.SetMode(gin.TestMode)
	previousMaster := common.IsMasterNode
	common.IsMasterNode = true
	t.Cleanup(func() { common.IsMasterNode = previousMaster })
	for _, tc := range []struct {
		kind, env string
		typ       common.DatabaseType
	}{
		{"sqlite", "", common.DatabaseTypeSQLite},
		{"mysql", "AUDIT_MYSQL_DSN", common.DatabaseTypeMySQL},
		{"postgres", "AUDIT_POSTGRES_DSN", common.DatabaseTypePostgreSQL},
		{"clickhouse", "AUDIT_CLICKHOUSE_DSN", common.DatabaseTypeClickHouse},
	} {
		t.Run(tc.kind, func(t *testing.T) {
			dsn := os.Getenv(tc.env)
			if tc.env != "" && dsn == "" {
				t.Skip(tc.env + " is not configured")
			}
			db, isolatedDSN := newAuditTestDatabase(t, tc.kind, dsn)
			mainDB, mainType := db, tc.typ
			if tc.kind == "clickhouse" {
				mainDB, _ = newAuditTestDatabase(t, "sqlite", "")
				mainType = common.DatabaseTypeSQLite
			}
			useLogExportDatabases(t, mainDB, db, mainType, tc.typ)
			require.NoError(t, mainDB.AutoMigrate(&model.Channel{}))
			if tc.kind == "clickhouse" {
				t.Setenv("LOG_SQL_DSN", isolatedDSN)
				require.NoError(t, model.InitLogDB())
			} else {
				require.NoError(t, db.AutoMigrate(&model.Log{}))
			}
			versionSQL := "SELECT version()"
			if tc.kind == "sqlite" {
				versionSQL = "SELECT sqlite_version()"
			}
			var version string
			require.NoError(t, model.LOG_DB.Raw(versionSQL).Scan(&version).Error)
			t.Logf("log database: %s %s", tc.kind, version)
			now := time.Now().Unix()
			seedExportLogs(t, mainDB, model.LOG_DB, now)
			query := fmt.Sprintf("start_timestamp=%d&end_timestamp=%d", now-2*24*3600, now+3600)

			listed := callLogHandler(t, GetUserLogs, 1, common.RoleCommonUser, query)[0]
			require.Equal(t, true, listed["success"])
			assert.EqualValues(t, 2, listed["data"].(map[string]any)["total"])
			listed = callLogHandler(t, GetAllLogs, 3, common.RoleAdminUser, query+"&username=alice")[0]
			require.Equal(t, true, listed["success"])
			assert.EqualValues(t, 2, listed["data"].(map[string]any)["total"])

			own := callLogHandler(t, ExportUserLogs, 1, common.RoleCommonUser, query)
			require.Len(t, own, 3)
			assert.Equal(t, []any{"a-2", "a-1"}, exportedRequestIds(own))
			assert.Equal(t, map[string]any{"success": true, "message": "", "data": map[string]any{"count": float64(2)}}, own[2])
			assert.Equal(t, float64(1), own[0]["id"], "user exports carry list positions, not row ids")
			assert.Empty(t, own[1]["channel_name"])
			assert.NotContains(t, own[1]["other"], "admin_info")

			all := callLogHandler(t, ExportAllLogs, 3, common.RoleAdminUser, query+"&model_name=gpt-%25")
			require.Len(t, all, 4)
			assert.Equal(t, []any{"a-2", "b-1", "a-1"}, exportedRequestIds(all))
			assert.Equal(t, "primary", all[2]["channel_name"])
			assert.Contains(t, all[2]["other"], "admin_info")
			assert.Equal(t, true, all[3]["success"])
		})
	}
}

func TestLogExportRules(t *testing.T) {
	gin.SetMode(gin.TestMode)
	db, _ := newAuditTestDatabase(t, "sqlite", "")
	useLogExportDatabases(t, db, db, common.DatabaseTypeSQLite, common.DatabaseTypeSQLite)
	require.NoError(t, db.AutoMigrate(&model.Channel{}, &model.Log{}, &model.QuotaData{}))
	now := time.Now().Unix()
	seedExportLogs(t, db, db, now)
	logSetting := operation_setting.GetLogSetting()
	previousSetting := *logSetting
	t.Cleanup(func() { *logSetting = previousSetting })
	recent := fmt.Sprintf("start_timestamp=%d&end_timestamp=%d", now-2*24*3600, now)
	tooLong := fmt.Sprintf("start_timestamp=%d&end_timestamp=%d", now-366*24*3600, now)

	t.Run("time range", func(t *testing.T) {
		rejected := callLogHandler(t, ExportAllLogs, 3, common.RoleAdminUser, tooLong)
		require.Len(t, rejected, 1)
		assert.Equal(t, false, rejected[0]["success"])
		assert.Equal(t, "The time range cannot exceed {{days}} days", rejected[0]["message_key"])
		assert.Equal(t, map[string]any{"days": float64(365)}, rejected[0]["message_params"])

		assert.Equal(t, false, callLogHandler(t, GetUserLogs, 1, common.RoleCommonUser, tooLong)[0]["success"])
		assert.Equal(t, false, callLogHandler(t, GetLogsSelfStat, 1, common.RoleCommonUser, tooLong)[0]["success"])
		assert.Equal(t, true, callLogHandler(t, GetUserLogs, 3, common.RoleAdminUser, tooLong)[0]["success"], "administrators search their own logs without a limit")

		// The dashboard follows the same range instead of its old one-month limit.
		fortyDays := fmt.Sprintf("start_timestamp=%d&end_timestamp=%d", now-40*24*3600, now)
		for _, handler := range []gin.HandlerFunc{GetUserQuotaDates, GetUserFlowQuotaDates} {
			assert.Equal(t, "The time range cannot exceed {{days}} days", callLogHandler(t, handler, 1, common.RoleCommonUser, tooLong)[0]["message_key"])
			assert.Equal(t, true, callLogHandler(t, handler, 1, common.RoleCommonUser, fortyDays)[0]["success"])
		}

		// Without a start, a user's search reaches back the whole range.
		listed := callLogHandler(t, GetUserLogs, 1, common.RoleCommonUser, "")[0]
		assert.Equal(t, true, listed["success"])
		assert.EqualValues(t, 2, listed["data"].(map[string]any)["total"])

		logSetting.MaxRangeDays = 400
		assert.Equal(t, true, callLogHandler(t, GetUserLogs, 1, common.RoleCommonUser, tooLong)[0]["success"])
		logSetting.MaxRangeDays = previousSetting.MaxRangeDays
	})

	t.Run("user export switch", func(t *testing.T) {
		logSetting.UserExportEnabled = false
		t.Cleanup(func() { logSetting.UserExportEnabled = previousSetting.UserExportEnabled })
		disabled := callLogHandler(t, ExportUserLogs, 1, common.RoleCommonUser, recent)
		require.Len(t, disabled, 1)
		assert.Equal(t, "Log export is disabled", disabled[0]["message_key"])
		assert.Equal(t, true, callLogHandler(t, ExportUserLogs, 3, common.RoleAdminUser, recent)[0]["success"], "administrators export their own logs even when users cannot")
	})

	t.Run("one export per user", func(t *testing.T) {
		release, ok, err := service.AcquireLogExportLock(1)
		require.NoError(t, err)
		require.True(t, ok)
		busy := callLogHandler(t, ExportUserLogs, 1, common.RoleCommonUser, recent)
		require.Len(t, busy, 1)
		assert.Equal(t, "Another log export is running. Try again after it finishes.", busy[0]["message_key"])
		other := callLogHandler(t, ExportUserLogs, 2, common.RoleCommonUser, recent)
		assert.Equal(t, true, other[len(other)-1]["success"], "other users are not blocked")
		release()
		assert.Len(t, callLogHandler(t, ExportUserLogs, 1, common.RoleCommonUser, recent), 3)
	})
}

func TestAcquireLogExportLock(t *testing.T) {
	for _, useRedis := range []bool{false, true} {
		t.Run(fmt.Sprintf("redis=%v", useRedis), func(t *testing.T) {
			previousRedis, previousClient := common.RedisEnabled, common.RDB
			common.RedisEnabled = useRedis
			if useRedis {
				server := miniredis.RunT(t)
				common.RDB = redis.NewClient(&redis.Options{Addr: server.Addr()})
				require.NoError(t, common.RDB.Ping(context.Background()).Err())
			}
			t.Cleanup(func() { common.RedisEnabled, common.RDB = previousRedis, previousClient })

			release, ok, err := service.AcquireLogExportLock(1)
			require.NoError(t, err)
			require.True(t, ok)
			_, ok, err = service.AcquireLogExportLock(1)
			require.NoError(t, err)
			assert.False(t, ok, "a second export of the same user must wait")
			releaseOther, ok, err := service.AcquireLogExportLock(2)
			require.NoError(t, err)
			assert.True(t, ok)
			releaseOther()
			release()
			releaseAgain, ok, err := service.AcquireLogExportLock(1)
			require.NoError(t, err)
			assert.True(t, ok, "a finished export frees the user")
			releaseAgain()
		})
	}
}

// The same assertions exercise the new columns and SQL aggregation on fresh
// and upgraded log databases. External instances are always loopback-only.
func TestUsageTagsDatabaseMatrix(t *testing.T) {
	gin.SetMode(gin.TestMode)
	previousConsume, previousExport := common.LogConsumeEnabled, common.DataExportEnabled
	common.LogConsumeEnabled, common.DataExportEnabled = true, false
	t.Cleanup(func() { common.LogConsumeEnabled, common.DataExportEnabled = previousConsume, previousExport })
	for _, tc := range []struct {
		kind, env string
		typ       common.DatabaseType
	}{
		{"sqlite", "", common.DatabaseTypeSQLite},
		{"mysql", "AUDIT_MYSQL_DSN", common.DatabaseTypeMySQL},
		{"postgres", "AUDIT_POSTGRES_DSN", common.DatabaseTypePostgreSQL},
	} {
		t.Run(tc.kind, func(t *testing.T) {
			dsn := os.Getenv(tc.env)
			if tc.env != "" && dsn == "" {
				t.Skip(tc.env + " is not configured")
			}
			for _, upgrade := range []bool{false, true} {
				t.Run(fmt.Sprintf("upgrade=%v", upgrade), func(t *testing.T) {
					db, _ := newAuditTestDatabase(t, tc.kind, dsn)
					mainDB, _ := newAuditTestDatabase(t, "sqlite", "")
					useLogExportDatabases(t, mainDB, db, common.DatabaseTypeSQLite, tc.typ)
					require.NoError(t, mainDB.AutoMigrate(&model.User{}))
					now := time.Now().Unix()
					if upgrade {
						require.NoError(t, db.AutoMigrate(&releasedAuditLog{}))
						require.NoError(t, db.Create(&releasedAuditLog{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "model", Quota: 7, RequestId: "legacy"}).Error)
					}
					for range 2 {
						require.NoError(t, db.AutoMigrate(&model.Log{}))
					}
					require.True(t, db.Migrator().HasIndex(&model.Log{}, "idx_logs_usage_tag"))
					require.True(t, db.Migrator().HasIndex(&model.Log{}, "idx_logs_request_id"))
					versionSQL := "SELECT version()"
					if tc.kind == "sqlite" {
						versionSQL = "SELECT sqlite_version()"
					}
					var version string
					require.NoError(t, db.Raw(versionSQL).Scan(&version).Error)
					t.Logf("log database: %s %s", tc.kind, version)
					rows := []model.Log{
						{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "model", UsageTag: "a_one", UsageRef: "r1", Quota: 20, RequestId: "first"},
						{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "model", UsageTag: "a_one", UsageRef: "r1", Quota: 30, RequestId: "second"},
						{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "image", UsageTag: "a_one", UsageRef: "r1", Quota: 5, RequestId: "image"},
						{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "model", UsageTag: "a_two", Quota: 10, RequestId: "other-space"},
						{UserId: 1, Username: "alice", CreatedAt: now, Type: model.LogTypeError, TokenName: "alice-token", ModelName: "model", UsageTag: "a_one", Quota: 999, RequestId: "error"},
						{UserId: 2, Username: "bob", CreatedAt: now, Type: model.LogTypeConsume, TokenName: "alice-token", ModelName: "model", UsageTag: "a_one", Quota: 999, RequestId: "another-user"},
					}
					require.NoError(t, db.Create(&rows).Error)
					query := fmt.Sprintf("type=2&token_name=alice-token&start_timestamp=%d&end_timestamp=%d", now-60, now+60)
					tags := callLogHandler(t, GetLogsSelfUsageTags, 1, common.RoleCommonUser, query)[0]
					require.Equal(t, true, tags["success"])
					groups := tags["data"].([]any)
					assert.Equal(t, map[string]any{"usage_tag": "a_one", "quota": float64(55), "count": float64(3)}, groups[0])
					assert.Equal(t, map[string]any{"usage_tag": "a_two", "quota": float64(10), "count": float64(1)}, groups[1])
					if upgrade {
						assert.Equal(t, map[string]any{"usage_tag": "", "quota": float64(7), "count": float64(1)}, groups[2])
					} else {
						assert.Len(t, groups, 2)
					}
					refs := callLogHandler(t, GetLogsSelfUsageRefs, 1, common.RoleCommonUser, query+"&usage_tag=a_one&model_name=model")[0]
					assert.Equal(t, []any{map[string]any{"usage_ref": "r1", "model_name": "model", "quota": float64(50), "count": float64(2)}}, refs["data"])
					refs = callLogHandler(t, GetLogsSelfUsageRefs, 1, common.RoleCommonUser, query+"&usage_tag=a_two")[0]
					assert.Equal(t, []any{map[string]any{"usage_ref": "", "model_name": "model", "quota": float64(10), "count": float64(1)}}, refs["data"])
					listed := callLogHandler(t, GetUserLogs, 1, common.RoleCommonUser, query+"&usage_tag=a_one")[0]
					assert.EqualValues(t, 3, listed["data"].(map[string]any)["total"])
					recorder := httptest.NewRecorder()
					ctx, _ := gin.CreateTestContext(recorder)
					ctx.Request = httptest.NewRequest(http.MethodGet, "/api/log/self/stat?"+query+"&usage_tag=a_two", nil)
					ctx.Set("id", 1)
					ctx.Set("username", "alice")
					ctx.Set("role", common.RoleCommonUser)
					GetLogsSelfStat(ctx)
					var stat struct{ Data struct{ Quota int } }
					require.NoError(t, common.Unmarshal(recorder.Body.Bytes(), &stat))
					assert.Equal(t, 10, stat.Data.Quota)
				})
			}
		})
	}
}

func TestUsageMarkersRecordedWithoutUpstreamHeaders(t *testing.T) {
	db, _ := newAuditTestDatabase(t, "sqlite", "")
	useLogExportDatabases(t, db, db, common.DatabaseTypeSQLite, common.DatabaseTypeSQLite)
	require.NoError(t, db.AutoMigrate(&model.User{}, &model.Log{}))
	previousConsume, previousExport := common.LogConsumeEnabled, common.DataExportEnabled
	common.LogConsumeEnabled, common.DataExportEnabled = true, false
	t.Cleanup(func() { common.LogConsumeEnabled, common.DataExportEnabled = previousConsume, previousExport })
	for _, tc := range []struct{ tag, ref, wantTag, wantRef string }{
		{"a_One.:-", "run_12", "a_One.:-", "run_12"},
		{"bad tag", strings.Repeat("r", 65), "", ""},
		{strings.Repeat("a", 64), "", strings.Repeat("a", 64), ""},
	} {
		t.Run(tc.tag, func(t *testing.T) {
			engine := gin.New()
			engine.Use(middleware.UsageTags())
			engine.POST("/call", func(c *gin.Context) {
				assert.Empty(t, c.GetHeader(common.UsageTagKey))
				assert.Empty(t, c.GetHeader(common.UsageRefKey))
				c.Set("username", "alice")
				c.Set(common.RequestIdKey, "request-"+tc.ref)
				model.RecordConsumeLog(c, 1, model.RecordConsumeLogParams{ModelName: "model", Quota: 20})
				model.RecordErrorLog(c, 1, 0, "model", "token", "failed", 0, 0, false, "", nil)
				c.Status(http.StatusNoContent)
			})
			request := httptest.NewRequest(http.MethodPost, "/call", nil)
			request.Header.Set(common.UsageTagKey, tc.tag)
			request.Header.Set(common.UsageRefKey, tc.ref)
			response := httptest.NewRecorder()
			engine.ServeHTTP(response, request)
			assert.Equal(t, http.StatusNoContent, response.Code)
			var logs []model.Log
			require.NoError(t, db.Order("id DESC").Limit(2).Find(&logs).Error)
			require.Len(t, logs, 2)
			for _, log := range logs {
				assert.Equal(t, tc.wantTag, log.UsageTag)
				assert.Equal(t, tc.wantRef, log.UsageRef)
			}
		})
	}
}

func TestUsageRefsLimitReturnsActionableError(t *testing.T) {
	db, _ := newAuditTestDatabase(t, "sqlite", "")
	useLogExportDatabases(t, db, db, common.DatabaseTypeSQLite, common.DatabaseTypeSQLite)
	require.NoError(t, db.AutoMigrate(&model.Log{}))
	logs := make([]model.Log, 50001)
	for i := range logs {
		logs[i] = model.Log{UserId: 1, Type: model.LogTypeConsume, UsageTag: "a_one", UsageRef: fmt.Sprintf("r%d", i), ModelName: "model", Quota: 1}
	}
	require.NoError(t, db.CreateInBatches(logs, 200).Error)
	body := callLogHandler(t, GetLogsSelfUsageRefs, 1, common.RoleAdminUser, "type=2&usage_tag=a_one")[0]
	assert.Equal(t, false, body["success"])
	assert.Equal(t, "usage_refs_limit_exceeded", body["code"])
	assert.Contains(t, body["message"], "shorten the time range")
}
