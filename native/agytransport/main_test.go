package main

import (
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestEndpointProfilesMatchCapturedTransportShape(t *testing.T) {
	pool := newTransportPool()
	cloudcode, err := pool.transport("cloudcode", "")
	if err != nil {
		t.Fatal(err)
	}
	if cloudcode.ForceAttemptHTTP2 {
		t.Fatal("cloudcode must use HTTP/1.1")
	}
	if cloudcode.TLSNextProto == nil || len(cloudcode.TLSNextProto) != 0 {
		t.Fatal("cloudcode must disable ALPN and HTTP/2")
	}
	oauth, err := pool.transport("oauth", "")
	if err != nil {
		t.Fatal(err)
	}
	if !oauth.ForceAttemptHTTP2 {
		t.Fatal("OAuth must negotiate HTTP/2")
	}
	if oauth.TLSNextProto != nil {
		t.Fatal("OAuth must retain the standard Go HTTP/2 transport")
	}
}

func TestRelayStreamsAndReusesUpstreamConnection(t *testing.T) {
	var newConnections atomic.Int32
	var requests atomic.Int32
	target := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Header.Get("X-Agy-Target-Url") != "" {
			t.Error("relay control header leaked upstream")
		}
		if r.Header.Get("User-Agent") != "antigravity/hub/test" {
			t.Errorf("unexpected User-Agent: %q", r.Header.Get("User-Agent"))
		}
		if r.Header.Get("Accept") != "" {
			t.Errorf("client Accept header must not be injected upstream: %q", r.Header.Get("Accept"))
		}
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		_, _ = io.WriteString(w, "data: first\n\n")
		flusher.Flush()
		_, _ = io.WriteString(w, "data: second\n\n")
	}))
	target.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			newConnections.Add(1)
		}
	}
	target.Start()
	defer target.Close()

	relay := &server{
		allowedHosts: map[string]bool{"127.0.0.1": true},
		allowHTTP:    true,
		pool:         newTransportPool(),
	}
	for range 2 {
		req, _ := http.NewRequest("POST", "http://relay.invalid/request", strings.NewReader("payload"))
		req.Header.Set("X-Agy-Target-Url", target.URL)
		req.Header.Set("X-Agy-Profile", "cloudcode")
		req.Header.Set("X-Agy-Timeout-Ms", "1000")
		req.Header.Set("User-Agent", "antigravity/hub/test")
		req.Header.Set("Accept", "*/*")
		recorder := httptest.NewRecorder()
		relay.ServeHTTP(recorder, req)
		response := recorder.Result()
		body, _ := io.ReadAll(response.Body)
		_ = response.Body.Close()
		if response.StatusCode != 200 || string(body) != "data: first\n\ndata: second\n\n" {
			t.Fatalf("unexpected relay response: %d %q", response.StatusCode, body)
		}
	}
	if requests.Load() != 2 {
		t.Fatalf("expected two upstream requests, got %d", requests.Load())
	}
	if newConnections.Load() != 1 {
		t.Fatalf("expected one reused upstream connection, got %d", newConnections.Load())
	}
}

func TestOAuthUsesStandardGoUserAgent(t *testing.T) {
	userAgent := make(chan string, 1)
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		userAgent <- r.Header.Get("User-Agent")
		_, _ = io.WriteString(w, "{}")
	}))
	defer target.Close()
	relay := &server{
		allowedHosts: map[string]bool{"127.0.0.1": true},
		allowHTTP:    true,
		pool:         newTransportPool(),
	}
	req, _ := http.NewRequest("POST", "http://relay.invalid/request", strings.NewReader("grant_type=refresh_token"))
	req.Header.Set("X-Agy-Target-Url", target.URL)
	req.Header.Set("X-Agy-Profile", "oauth")
	req.Header.Set("X-Agy-Timeout-Ms", "1000")
	req.Header.Set("User-Agent", "vscode/1.X.X (Antigravity/2.28.2)")
	recorder := httptest.NewRecorder()
	relay.ServeHTTP(recorder, req)
	if got := <-userAgent; got != "Go-http-client/1.1" {
		t.Fatalf("OAuth must use the default Go User-Agent, got %q", got)
	}
}

func TestRejectsDisallowedHostWithoutLeakingProxyCredentials(t *testing.T) {
	relay := &server{
		allowedHosts: map[string]bool{"allowed.invalid": true},
		pool:         newTransportPool(),
	}
	req, _ := http.NewRequest("POST", "http://relay.invalid/request", nil)
	req.Header.Set("X-Agy-Target-Url", "https://blocked.invalid/token")
	req.Header.Set("X-Agy-Proxy-Url", "http://user:secret@proxy.invalid:8080")
	req.Header.Set("X-Agy-Profile", "oauth")
	req.Header.Set("X-Agy-Timeout-Ms", "1000")
	recorder := httptest.NewRecorder()
	relay.ServeHTTP(recorder, req)
	body, _ := io.ReadAll(recorder.Result().Body)
	if recorder.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", recorder.Code)
	}
	if strings.Contains(string(body), "secret") || strings.Contains(string(body), "proxy.invalid") {
		t.Fatalf("response leaked proxy details: %q", body)
	}
}

func TestRelayDoesNotFollowRedirectsOutsideAllowlist(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Location", "https://blocked.invalid/redirected")
		w.WriteHeader(http.StatusFound)
	}))
	defer target.Close()
	relay := &server{
		allowedHosts: map[string]bool{"127.0.0.1": true},
		allowHTTP:    true,
		pool:         newTransportPool(),
	}
	req, _ := http.NewRequest("GET", "http://relay.invalid/request", nil)
	req.Header.Set("X-Agy-Target-Url", target.URL)
	req.Header.Set("X-Agy-Profile", "cloudcode")
	recorder := httptest.NewRecorder()
	relay.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusFound {
		t.Fatalf("redirect must be returned instead of followed, got %d", recorder.Code)
	}
}

func TestTransportPoolIsBounded(t *testing.T) {
	pool := newTransportPool()
	pool.maxEntries = 2
	for _, proxy := range []string{"", "http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"} {
		if _, err := pool.transport("cloudcode", proxy); err != nil {
			t.Fatal(err)
		}
	}
	pool.evictIdle(time.Now().Add(time.Hour))
	pool.mu.Lock()
	defer pool.mu.Unlock()
	if len(pool.entries) > 2 {
		t.Fatalf("pool exceeded bound: %d", len(pool.entries))
	}
}
