package constant

func SearchChannelType(model string) int {
	switch model {
	case "tavily-search", "tavily-search-advanced":
		return ChannelTypeTavily
	case "brave-search":
		return ChannelTypeBraveSearch
	case "bocha-search":
		return ChannelTypeBochaSearch
	case "baidu-search":
		return ChannelTypeBaiduSearch
	default:
		return ChannelTypeUnknown
	}
}

func SearchModels(channelType int) []string {
	switch channelType {
	case ChannelTypeTavily:
		return []string{"tavily-search", "tavily-search-advanced"}
	case ChannelTypeBraveSearch:
		return []string{"brave-search"}
	case ChannelTypeBochaSearch:
		return []string{"bocha-search"}
	case ChannelTypeBaiduSearch:
		return []string{"baidu-search"}
	default:
		return nil
	}
}

func IsSearchChannel(channelType int) bool {
	return channelType == ChannelTypeTavily || channelType == ChannelTypeBraveSearch || channelType == ChannelTypeBochaSearch || channelType == ChannelTypeBaiduSearch
}
