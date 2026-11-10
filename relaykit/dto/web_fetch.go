package dto

import (
	"net/http"

	"github.com/QuantumNous/new-api/relaykit/types"
)

type WebFetchRequest struct {
	Model string `json:"model"`
	URL   string `json:"url"`
}

func (r *WebFetchRequest) GetTokenCountMeta() *types.TokenCountMeta {
	return &types.TokenCountMeta{TokenType: types.TokenTypeTokenizer}
}

func (r *WebFetchRequest) IsStream(_ *http.Request) bool { return false }

func (r *WebFetchRequest) SetModelName(model string) { r.Model = model }

type WebFetchResponse struct {
	URL         string `json:"url"`
	Status      int    `json:"status"`
	ContentType string `json:"content_type"`
	Body        []byte `json:"body"`
}
