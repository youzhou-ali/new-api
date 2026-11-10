package common

import "github.com/QuantumNous/new-api/relaykit/dto"

// MatchAdvancedCustomRequestRoute lets the browser playground use standard
// chat routes while honoring any explicitly configured playground route.
func MatchAdvancedCustomRequestRoute(config *dto.AdvancedCustomConfig, requestPath, modelName string) (dto.AdvancedCustomRoute, bool) {
	route, matched := config.MatchPathForModel(requestPath, modelName)
	if !matched && requestPath == "/pg/chat/completions" {
		return config.MatchPathForModel("/v1/chat/completions", modelName)
	}
	return route, matched
}
