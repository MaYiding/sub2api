package ailog

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestTransportCapturesSeparateRetryAttemptsWithoutSecrets(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error(err)
		}
		w.WriteHeader(http.StatusTooManyRequests)
		if _, err := io.WriteString(w, `{"error":"retry"}`); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	sink := &testSink{}
	capture := New(sink, "test", "run", nil)
	client := &http.Client{Transport: &Transport{Base: http.DefaultTransport}}
	for i := 0; i < 2; i++ {
		req, err := http.NewRequest(http.MethodPost, server.URL+"?api_key=secret", strings.NewReader(`{"input":"hello"}`))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer secret")
		req = req.WithContext(WithContext(req.Context(), capture))
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		if err := resp.Body.Close(); err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != http.StatusTooManyRequests || string(body) != `{"error":"retry"}` {
			t.Fatal("transport behavior changed")
		}
	}
	capture.Finish(nil)
	attempts := map[any]bool{}
	for _, event := range sink.events {
		if event.Kind == "upstream.start" {
			attempts[event.Metadata["attempt_id"]] = true
		}
		for _, value := range event.Metadata {
			if s, ok := value.(string); ok && strings.Contains(s, "secret") {
				t.Fatal("upstream credentials captured")
			}
		}
	}
	if len(attempts) != 2 {
		t.Fatal("retries collapsed")
	}
}
