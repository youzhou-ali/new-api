package model

import (
	"slices"
	"strings"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/constant"
	"github.com/QuantumNous/new-api/dto"
)

var filterEvalOrder = []dto.ChannelFilterKind{
	dto.FilterRequestPath,
	dto.FilterTaskPluginEndpoint,
	dto.FilterTaskPluginIdentity,
	dto.FilterResponsesWebSocket,
}

// ChannelSatisfiesFilters reports whether ch passes every filter.
// On false, it returns the first violated filter in filterEvalOrder for error
// attribution.
func ChannelSatisfiesFilters(ch *Channel, modelName string, filters []dto.ChannelFilter) (bool, dto.ChannelFilterKind) {
	if ch == nil {
		return false, ""
	}
	for _, kind := range filterEvalOrder {
		for _, filter := range filters {
			if filter.Kind != kind {
				continue
			}
			if !channelMatchesFilter(ch, modelName, filter) {
				return false, kind
			}
		}
	}
	return true, ""
}

// filterCandidateIDs applies filters to a cached candidate id list.
// Caller must hold channelSyncLock (read lock). The input slice is never mutated.
// A missing id in channelsIDM is kept for request_path (downstream consistency
// error) and dropped for the other filters.
func filterCandidateIDs(ids []int, modelName string, filters []dto.ChannelFilter) (kept []int, emptiedBy dto.ChannelFilterKind) {
	if len(ids) == 0 {
		return ids, ""
	}
	kept = ids
	for _, kind := range filterEvalOrder {
		kindFilters := filtersByKind(filters, kind)
		if len(kindFilters) == 0 {
			continue
		}
		next := make([]int, 0, len(kept))
		for _, id := range kept {
			channel, exists := channelsIDM[id]
			if candidatePassesKindFilters(channel, exists, modelName, kind, kindFilters) {
				next = append(next, id)
			}
		}
		if len(kept) > 0 && len(next) == 0 {
			return next, kind
		}
		kept = next
	}
	return kept, ""
}

func filtersByKind(filters []dto.ChannelFilter, kind dto.ChannelFilterKind) []dto.ChannelFilter {
	var matched []dto.ChannelFilter
	for _, filter := range filters {
		if filter.Kind == kind {
			matched = append(matched, filter)
		}
	}
	return matched
}

func candidatePassesKindFilters(ch *Channel, exists bool, modelName string, kind dto.ChannelFilterKind, filters []dto.ChannelFilter) bool {
	if kind == dto.FilterRequestPath && !exists {
		return true
	}
	if !exists || ch == nil {
		return false
	}
	for _, filter := range filters {
		if !channelMatchesFilter(ch, modelName, filter) {
			return false
		}
	}
	return true
}

func channelMatchesFilter(ch *Channel, modelName string, filter dto.ChannelFilter) bool {
	switch filter.Kind {
	case dto.FilterRequestPath:
		if filter.RequestPath == "/v1/search" {
			return constant.SearchChannelType(modelName) != constant.ChannelTypeUnknown &&
				constant.SearchChannelType(modelName) == ch.Type && ch.ValidateSearchConfiguration() == nil
		}
		if constant.IsSearchChannel(ch.Type) {
			return false
		}
		if filter.RequestPath == "" {
			return true
		}
		if !constant.IsAdvancedCustomChannel(ch.Type) {
			return true
		}
		config := ch.GetOtherSettings().AdvancedCustom
		return config != nil && config.SupportsPathForModel(filter.RequestPath, modelName)
	case dto.FilterTaskPluginIdentity:
		if filter.TaskPluginKey == "" {
			return ch.Type != constant.ChannelTypeTaskPlugin
		}
		if ch.Type == constant.ChannelTypeTaskPlugin || ch.Type == constant.ChannelTypeNewAPI {
			// A New API channel serves every plugin it is extended with; the
			// pinned plugin or any shared-model candidate may execute there.
			setting := ch.GetSetting()
			return setting.BindsTaskPlugin(filter.TaskPluginKey) || slices.ContainsFunc(filter.TaskPluginKeys, setting.BindsTaskPlugin)
		}
		return slices.Contains(filter.TaskPluginChannelTypes, ch.Type)
	case dto.FilterTaskPluginEndpoint:
		pluginChannel := slices.Contains(filter.TaskPluginChannelTypes, ch.Type)
		if ch.Type == constant.ChannelTypeTaskPlugin || ch.Type == constant.ChannelTypeNewAPI {
			pluginChannel = slices.ContainsFunc(filter.TaskPluginKeys, ch.GetSetting().BindsTaskPlugin)
		}
		if filter.TaskPluginRelayOnly {
			return ch.Type != constant.ChannelTypeTaskPlugin && !pluginChannel
		}
		// Ordinary channels and matching plugin channels compete using the same
		// group, priority, weight, affinity, and pin rules. Unrelated type-61
		// plugins cannot execute this endpoint.
		return ch.Type != constant.ChannelTypeTaskPlugin || pluginChannel
	case dto.FilterResponsesWebSocket:
		if !ch.GetSetting().ResponsesWebSocketEnabled {
			return false
		}
		switch ch.Type {
		case constant.ChannelTypeOpenAI, constant.ChannelTypeCodex, constant.ChannelTypeSub2API, constant.ChannelTypeNewAPI:
			return true
		case constant.ChannelTypeAdvancedCustom:
			// The session forwards native Responses events without protocol
			// conversion, so only a converter-free /v1/responses route qualifies.
			route, ok := ch.GetOtherSettings().AdvancedCustom.MatchPathForModel("/v1/responses", modelName)
			return ok && route.IsNative()
		default:
			return false
		}
	default:
		return true
	}
}

// ValidateSearchConfiguration keeps a search product's source and cost tier
// stable across channel selection, pinning, retries, and administrator edits.
func (ch *Channel) ValidateSearchConfiguration() error {
	if !constant.IsSearchChannel(ch.Type) {
		return nil
	}
	for _, name := range ch.GetModels() {
		if constant.SearchChannelType(name) != ch.Type {
			return common.NewMessage("Search channel models must belong to its search provider")
		}
	}
	mapping := strings.TrimSpace(ch.GetModelMapping())
	if mapping != "" {
		var models map[string]string
		if err := common.UnmarshalJsonStr(mapping, &models); err != nil {
			return common.NewMessage("Invalid search model mapping")
		}
		for name, target := range models {
			if target != "" && name != target {
				return common.NewMessage("Search products cannot be changed through model mapping")
			}
		}
	}
	var overrides map[string]any
	if ch.ParamOverride != nil && strings.TrimSpace(*ch.ParamOverride) != "" {
		if err := common.UnmarshalJsonStr(*ch.ParamOverride, &overrides); err != nil {
			return common.NewMessage("Search channels do not support parameter overrides or body passthrough")
		}
	}
	if len(overrides) != 0 || ch.GetSetting().PassThroughBodyEnabled {
		return common.NewMessage("Search channels do not support parameter overrides or body passthrough")
	}
	return nil
}

// ValidateSearchUpdate checks the resulting configuration using the nonzero
// field and non-nil pointer semantics of Channel.Update.
func (ch *Channel) ValidateSearchUpdate(update *Channel) error {
	if !constant.IsSearchChannel(ch.Type) && !constant.IsSearchChannel(update.Type) {
		return nil
	}
	merged := *ch
	if update.Type != 0 {
		merged.Type = update.Type
	}
	if update.Models != "" {
		merged.Models = update.Models
	}
	if update.ModelMapping != nil {
		merged.ModelMapping = update.ModelMapping
	}
	if update.ParamOverride != nil {
		merged.ParamOverride = update.ParamOverride
	}
	if update.Setting != nil {
		merged.Setting = update.Setting
	}
	return merged.ValidateSearchConfiguration()
}
