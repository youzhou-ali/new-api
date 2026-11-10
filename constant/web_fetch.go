package constant

const WebFetchModel = "web-fetch"

// IsToolChannel reports channels that serve a tool endpoint instead of LLM paths.
func IsToolChannel(channelType int) bool {
	return IsSearchChannel(channelType) || channelType == ChannelTypeWebFetch
}

func ToolChannelModels(channelType int) []string {
	if channelType == ChannelTypeWebFetch {
		return []string{WebFetchModel}
	}
	return SearchModels(channelType)
}
