package service

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/QuantumNous/new-api/common"
	"github.com/QuantumNous/new-api/i18n"
	"github.com/QuantumNous/new-api/relaykit/types"
)

const (
	WebFetchTimeout      = 30 * time.Second
	webFetchMaxBodyBytes = 10 << 20
	webFetchMaxRedirects = 5
	webFetchMaxURLLength = 4000
	webFetchMaxLogPath   = 256
	webFetchUserAgent    = "Mozilla/5.0 (compatible; CubeAgent/relay)"
	webFetchAccept       = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
)

var errWebFetchBlocked = errors.New("web fetch target is not a public address")

type WebFetchResult struct {
	URL         string
	Status      int
	ContentType string
	Body        []byte
	Host        string
	Path        string
}

type WebFetchError struct {
	Code       types.ErrorCode
	StatusCode int
	MessageKey string
	Host       string
	Path       string
	Detail     string // Server diagnostics only. Never include raw URL or header values.
}

type webFetcher struct {
	transport *http.Transport
	allowPort func(string) bool
}

var webFetchProtection common.SSRFProtection
var defaultWebFetcher = newWebFetcher(webFetchProtection.IsIPAccessAllowed, func(port string) bool {
	return port == "" || port == "80" || port == "443"
}, nil)
var testWebFetcher atomic.Pointer[webFetcher]

func newWebFetcher(allowIP func(net.IP) bool, allowPort func(string) bool, resolver *net.Resolver) *webFetcher {
	dialer := &net.Dialer{
		Resolver: resolver,
		ControlContext: func(_ context.Context, _, address string, _ syscall.RawConn) error {
			// Validate the actual address selected by the dialer after DNS resolution.
			host, _, err := net.SplitHostPort(address)
			if err != nil || !allowIP(net.ParseIP(host)) {
				return errWebFetchBlocked
			}
			return nil
		},
	}
	return &webFetcher{
		allowPort: allowPort,
		transport: &http.Transport{
			DialContext:            dialer.DialContext,
			ForceAttemptHTTP2:      true,
			DisableKeepAlives:      true,
			MaxResponseHeaderBytes: 1 << 20,
		},
	}
}

// AllowWebFetchLoopbackForTest lets tests reach httptest servers on any port.
// Every other address still follows the fixed IP policy. DNS queries fail
// locally. Tests using this override must not run in parallel with tests of
// the production policy.
func AllowWebFetchLoopbackForTest() (restore func()) {
	resolver := &net.Resolver{
		PreferGo: true,
		Dial: func(context.Context, string, string) (net.Conn, error) {
			return nil, errors.New("DNS is disabled in web fetch tests")
		},
	}
	fetcher := newWebFetcher(func(ip net.IP) bool {
		return ip.IsLoopback() || webFetchProtection.IsIPAccessAllowed(ip)
	}, func(string) bool { return true }, resolver)
	previous := testWebFetcher.Swap(fetcher)
	return func() {
		testWebFetcher.Store(previous)
		fetcher.transport.CloseIdleConnections()
	}
}

func WebFetch(ctx context.Context, rawURL string) (*WebFetchResult, *WebFetchError) {
	fetcher := testWebFetcher.Load()
	if fetcher == nil {
		fetcher = defaultWebFetcher
	}
	return fetcher.fetch(ctx, rawURL)
}

func (f *webFetcher) fetch(ctx context.Context, rawURL string) (*WebFetchResult, *WebFetchError) {
	invalidURL := &WebFetchError{
		Code: "invalid_url", StatusCode: http.StatusBadRequest, MessageKey: i18n.MsgWebFetchInvalidURL,
	}
	for redirects := 0; ; redirects++ {
		if utf8.RuneCountInString(rawURL) > webFetchMaxURLLength {
			return nil, invalidURL
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil || (req.URL.Scheme != "http" && req.URL.Scheme != "https") ||
			req.URL.User != nil || req.URL.Opaque != "" || req.URL.Hostname() == "" ||
			!f.allowPort(req.URL.Port()) {
			return nil, invalidURL
		}
		host, path := req.URL.Hostname(), req.URL.EscapedPath()
		path = path[:min(len(path), webFetchMaxLogPath)]
		req.Header.Set("User-Agent", webFetchUserAgent)
		req.Header.Set("Accept", webFetchAccept)

		// RoundTrip leaves redirects to this loop, so no Referer is added.
		resp, err := f.transport.RoundTrip(req)
		if err != nil {
			return nil, webFetchNetworkError(ctx, err, host, path)
		}
		switch resp.StatusCode {
		case http.StatusMovedPermanently, http.StatusFound, http.StatusSeeOther,
			http.StatusTemporaryRedirect, http.StatusPermanentRedirect:
			if location := resp.Header.Get("Location"); location != "" {
				resp.Body.Close()
				if redirects == webFetchMaxRedirects {
					return nil, &WebFetchError{
						Code: "too_many_redirects", StatusCode: http.StatusBadGateway,
						MessageKey: i18n.MsgWebFetchTooManyRedirects, Host: host, Path: path,
					}
				}
				next, err := req.URL.Parse(location)
				if err != nil {
					return nil, invalidURL
				}
				rawURL = next.String()
				continue
			}
		}

		readLimit := int64(webFetchMaxBodyBytes + 1)
		unsupportedEncoding := false
		for value := range strings.SplitSeq(strings.Join(resp.Header.Values("Content-Encoding"), ","), ",") {
			encoding := strings.TrimSpace(value)
			if encoding != "" && !strings.EqualFold(encoding, "identity") {
				unsupportedEncoding = true
				readLimit = 1 // An unsupported encoding is permitted only on an empty body.
				break
			}
		}
		if resp.StatusCode == http.StatusSwitchingProtocols {
			readLimit = 0 // A protocol switch has no HTTP body to read.
		}
		body, err := io.ReadAll(io.LimitReader(resp.Body, readLimit))
		resp.Body.Close()
		if err != nil {
			return nil, webFetchNetworkError(ctx, err, host, path)
		}
		if unsupportedEncoding && len(body) > 0 {
			return nil, &WebFetchError{
				Code: "unsupported_encoding", StatusCode: http.StatusBadGateway,
				MessageKey: i18n.MsgWebFetchUnsupportedEncoding, Host: host, Path: path,
			}
		}
		if len(body) > webFetchMaxBodyBytes {
			return nil, &WebFetchError{
				Code: "too_large", StatusCode: http.StatusBadGateway,
				MessageKey: i18n.MsgWebFetchTooLarge, Host: host, Path: path,
			}
		}
		return &WebFetchResult{
			URL: req.URL.String(), Status: resp.StatusCode, ContentType: resp.Header.Get("Content-Type"),
			Body: body, Host: host, Path: path,
		}, nil
	}
}

func webFetchNetworkError(ctx context.Context, err error, host, path string) *WebFetchError {
	failure := &WebFetchError{Host: host, Path: path}
	var dnsErr *net.DNSError
	switch {
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		failure.Code, failure.StatusCode, failure.MessageKey = "timeout", http.StatusGatewayTimeout, i18n.MsgWebFetchTimeout
	case errors.Is(err, errWebFetchBlocked):
		failure.Code, failure.StatusCode, failure.MessageKey = "blocked_address", http.StatusBadRequest, i18n.MsgWebFetchBlockedAddress
	case errors.As(err, &dnsErr):
		failure.Code, failure.StatusCode, failure.MessageKey = "dns_failed", http.StatusBadGateway, i18n.MsgWebFetchDNSFailed
	default:
		failure.Code, failure.StatusCode, failure.MessageKey = "connect_failed", http.StatusBadGateway, i18n.MsgWebFetchConnectFailed
	}
	// Only log messages from network and certificate errors. Other errors
	// can contain URL components or data sent by the target.
	var (
		opErr            *net.OpError
		verificationErr  *tls.CertificateVerificationError
		unknownAuthority x509.UnknownAuthorityError
		hostnameErr      x509.HostnameError
		invalidCert      x509.CertificateInvalidError
	)
	switch {
	case errors.As(err, &opErr):
		failure.Detail = opErr.Error()
	case errors.As(err, &verificationErr):
		failure.Detail = verificationErr.Error()
	case errors.As(err, &unknownAuthority):
		failure.Detail = unknownAuthority.Error()
	case errors.As(err, &hostnameErr):
		failure.Detail = hostnameErr.Error()
	case errors.As(err, &invalidCert):
		failure.Detail = invalidCert.Error()
	default:
		failure.Detail = fmt.Sprintf("%T", err)
	}
	return failure
}
