package ailog

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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

type bodyTestSink struct{ testSink }

func (s *bodyTestSink) UpstreamBodies() bool { return true }

func TestTransportSummarizesUpstreamBodiesUnlessEnabled(t *testing.T) {
	const reply = `{"output":"hi"}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		_, _ = io.WriteString(w, reply)
	}))
	defer server.Close()
	sum := sha256.Sum256([]byte(reply))
	for _, copied := range []bool{false, true} {
		var sink Sink = &testSink{}
		if copied {
			sink = &bodyTestSink{}
		}
		capture := New(sink, "test", "run", nil)
		req, err := http.NewRequest(http.MethodPost, server.URL, strings.NewReader(`{"input":"hello"}`))
		if err != nil {
			t.Fatal(err)
		}
		resp, err := (&http.Client{Transport: &Transport{Base: http.DefaultTransport}}).Do(req.WithContext(WithContext(req.Context(), capture)))
		if err != nil {
			t.Fatal(err)
		}
		if body, err := io.ReadAll(resp.Body); err != nil || string(body) != reply {
			t.Fatal("upstream response changed")
		}
		_ = resp.Body.Close()
		capture.Finish(nil)
		var events []Event
		if copied {
			events = sink.(*bodyTestSink).events
		} else {
			events = sink.(*testSink).events
		}
		if events[0].Metadata["upstream_bodies"] != copied {
			t.Fatal("start does not record upstream body mode")
		}
		var body string
		var end map[string]any
		for _, e := range events {
			switch e.Kind {
			case "upstream.response.body":
				data, _ := base64.StdEncoding.DecodeString(e.Data)
				body += string(data)
			case "upstream.response.end":
				end = e.Metadata
			}
		}
		if copied != (body == reply) || (!copied && body != "") {
			t.Fatalf("upstream body copied=%v got %q", copied, body)
		}
		if end["bytes"] != int64(len(reply)) || end["sha256"] != hex.EncodeToString(sum[:]) || end["body_complete"] != true || end["body_copied"] != copied {
			t.Fatalf("upstream summary wrong: %v", end)
		}
	}
}
