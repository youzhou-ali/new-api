package dto

import (
	"net/http"

	"github.com/QuantumNous/new-api/relaykit/types"
)

// SearchRequest describes one non-streaming web search, not an LLM completion.
type SearchRequest struct {
	Model      string  `json:"model"`
	Query      string  `json:"query"`
	MaxResults *int    `json:"max_results,omitempty"`
	Freshness  *string `json:"freshness,omitempty"`
}

func (r *SearchRequest) GetTokenCountMeta() *types.TokenCountMeta {
	return &types.TokenCountMeta{CombineText: r.Query, TokenType: types.TokenTypeTokenizer}
}

func (r *SearchRequest) IsStream(_ *http.Request) bool { return false }

func (r *SearchRequest) SetModelName(model string) { r.Model = model }

type SearchResult struct {
	Title         string `json:"title"`
	URL           string `json:"url"`
	Content       string `json:"content"`
	PublishedDate string `json:"published_date,omitempty"`
	SiteName      string `json:"site_name,omitempty"`
}

type SearchResponse struct {
	ID      string         `json:"id"`
	Model   string         `json:"model"`
	Query   string         `json:"query"`
	Results []SearchResult `json:"results"`
}
