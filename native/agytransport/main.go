package main

import (
	"context"
	"crypto/tls"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const maxRequestBody = 256 << 20

type transportEntry struct {
	transport *http.Transport
	lastUsed  time.Time
}

type transportPool struct {
	mu         sync.Mutex
	entries    map[string]*transportEntry
	maxEntries int
}

func newTransportPool() *transportPool {
	return &transportPool{entries: make(map[string]*transportEntry), maxEntries: 128}
}

func proxyFunc(raw string) (func(*http.Request) (*url.URL, error), error) {
	if raw == "" {
		return nil, nil
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("invalid proxy")
	}
	switch parsed.Scheme {
	case "http", "https", "socks5":
	case "socks5h":
		parsed.Scheme = "socks5"
	default:
		return nil, fmt.Errorf("unsupported proxy")
	}
	return http.ProxyURL(parsed), nil
}

func (p *transportPool) transport(profile, proxyURL string) (*http.Transport, error) {
	if profile != "oauth" && profile != "cloudcode" {
		return nil, fmt.Errorf("invalid profile")
	}
	proxy, err := proxyFunc(proxyURL)
	if err != nil {
		return nil, err
	}
	key := profile + "\x00" + proxyURL
	now := time.Now()
	p.evictIdle(now)
	p.mu.Lock()
	defer p.mu.Unlock()
	if entry := p.entries[key]; entry != nil {
		entry.lastUsed = now
		return entry.transport, nil
	}
	if len(p.entries) >= p.maxEntries {
		p.evictOldestLocked()
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = proxy
	if profile == "oauth" {
		transport.ForceAttemptHTTP2 = true
	} else {
		transport.ForceAttemptHTTP2 = false
		transport.TLSClientConfig = &tls.Config{}
		transport.TLSNextProto = make(map[string]func(string, *tls.Conn) http.RoundTripper)
	}
	p.entries[key] = &transportEntry{transport: transport, lastUsed: now}
	return transport, nil
}

func (p *transportPool) evictOldestLocked() {
	var oldestKey string
	var oldest time.Time
	for key, entry := range p.entries {
		if oldestKey == "" || entry.lastUsed.Before(oldest) {
			oldestKey, oldest = key, entry.lastUsed
		}
	}
	if oldestKey != "" {
		p.entries[oldestKey].transport.CloseIdleConnections()
		delete(p.entries, oldestKey)
	}
}

func (p *transportPool) evictIdle(now time.Time) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for key, entry := range p.entries {
		if now.Sub(entry.lastUsed) > 15*time.Minute {
			entry.transport.CloseIdleConnections()
			delete(p.entries, key)
		}
	}
	for len(p.entries) > p.maxEntries {
		p.evictOldestLocked()
	}
}

type server struct {
	allowedHosts map[string]bool
	allowHTTP    bool
	pool         *transportPool
}

var hopHeaders = map[string]bool{
	"Connection":          true,
	"Proxy-Connection":    true,
	"Keep-Alive":          true,
	"Proxy-Authenticate":  true,
	"Proxy-Authorization": true,
	"Te":                  true,
	"Trailer":             true,
	"Transfer-Encoding":   true,
	"Upgrade":             true,
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/request" {
		http.NotFound(w, r)
		return
	}
	target, err := url.Parse(r.Header.Get("X-Agy-Target-Url"))
	if err != nil || target.Hostname() == "" || !s.allowedHosts[target.Hostname()] {
		http.Error(w, "target host is not allowed", http.StatusForbidden)
		return
	}
	if target.Scheme != "https" && !(s.allowHTTP && target.Scheme == "http") {
		http.Error(w, "target scheme is not allowed", http.StatusForbidden)
		return
	}
	profile := r.Header.Get("X-Agy-Profile")
	transport, err := s.pool.transport(profile, r.Header.Get("X-Agy-Proxy-Url"))
	if err != nil {
		http.Error(w, "invalid transport configuration", http.StatusBadRequest)
		return
	}
	timeoutMS, _ := strconv.Atoi(r.Header.Get("X-Agy-Timeout-Ms"))
	if timeoutMS <= 0 {
		timeoutMS = 30000
	}
	timeout := time.Duration(timeoutMS) * time.Millisecond
	if r.Header.Get("X-Agy-Streaming") == "1" {
		timeout = 0
	}

	var body io.ReadCloser = http.NoBody
	if r.Body != nil {
		body = http.MaxBytesReader(w, r.Body, maxRequestBody)
	}
	upstream, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), body)
	if err != nil {
		http.Error(w, "invalid upstream request", http.StatusBadRequest)
		return
	}
	upstream.ContentLength = r.ContentLength
	for name, values := range r.Header {
		if strings.HasPrefix(name, "X-Agy-") || hopHeaders[name] {
			continue
		}
		for _, value := range values {
			upstream.Header.Add(name, value)
		}
	}
	upstream.Header.Del("Accept")
	upstream.Header.Del("Accept-Encoding")
	upstream.Header.Del("Content-Length")
	if profile == "oauth" {
		upstream.Header.Del("User-Agent")
	}

	client := &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Do(upstream)
	if err != nil {
		http.Error(w, "upstream request failed", http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	for name, values := range response.Header {
		if hopHeaders[name] {
			continue
		}
		for _, value := range values {
			w.Header().Add(name, value)
		}
	}
	w.WriteHeader(response.StatusCode)
	flusher, _ := w.(http.Flusher)
	buffer := make([]byte, 32*1024)
	for {
		read, readErr := response.Body.Read(buffer)
		if read > 0 {
			if _, writeErr := w.Write(buffer[:read]); writeErr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr == io.EOF {
			return
		}
		if readErr != nil {
			return
		}
	}
}

func main() {
	socketPath := flag.String("socket", "", "Unix socket path")
	allowed := flag.String("allowed-hosts", "oauth2.googleapis.com,www.googleapis.com,daily-cloudcode-pa.googleapis.com,cloudcode-pa.googleapis.com,generativelanguage.googleapis.com", "comma-separated target hosts")
	allowHTTP := flag.Bool("allow-http", false, "allow plaintext upstream URLs for local tests")
	flag.Parse()
	if *socketPath == "" {
		return
	}
	allowedHosts := make(map[string]bool)
	for _, host := range strings.Split(*allowed, ",") {
		if host = strings.TrimSpace(host); host != "" {
			allowedHosts[host] = true
		}
	}
	_ = os.Remove(*socketPath)
	listener, err := net.Listen("unix", *socketPath)
	if err != nil {
		return
	}
	defer listener.Close()
	if err := os.Chmod(*socketPath, 0o600); err != nil {
		return
	}
	application := &http.Server{
		Handler:           &server{allowedHosts: allowedHosts, allowHTTP: *allowHTTP, pool: newTransportPool()},
		ReadHeaderTimeout: 30 * time.Second,
	}
	stopped := make(chan os.Signal, 1)
	signal.Notify(stopped, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stopped
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = application.Shutdown(ctx)
	}()
	_ = application.Serve(listener)
}
