package ailog

import (
	"crypto/sha256"
	"encoding/hex"
	"hash"
	"io"
	"net/http"
	"sync"

	"github.com/google/uuid"
)

// Transport captures each actual upstream HTTP attempt, including failed
// account retries. Authorization, cookies and query strings are excluded.
// Attempt bodies are copied only when the sink opts in; otherwise each body
// is summarized by its byte count and SHA-256 in the matching `.end` event.
type Transport struct{ Base http.RoundTripper }

func (t *Transport) RoundTrip(req *http.Request) (*http.Response, error) {
	c := FromContext(req.Context())
	if c == nil {
		return t.Base.RoundTrip(req)
	}
	attempt := uuid.NewString()
	c.Record("upstream.start", nil, map[string]any{
		"attempt_id": attempt, "method": req.Method, "host": req.URL.Hostname(),
		"content_type": req.Header.Get("Content-Type"), "content_encoding": req.Header.Get("Content-Encoding"),
	})
	clone := req.Clone(req.Context())
	if req.Body != nil {
		clone.Body = newHTTPBody(req.Body, c, "upstream.request", attempt)
	}
	resp, err := t.Base.RoundTrip(clone)
	if err != nil {
		// Transport errors may embed credential-bearing URLs; retain a category.
		c.Record("upstream.error", nil, map[string]any{"attempt_id": attempt, "transport_error": true})
		return resp, err
	}
	c.Record("upstream.headers", nil, map[string]any{
		"attempt_id": attempt, "status": resp.StatusCode,
		"content_type": resp.Header.Get("Content-Type"), "content_encoding": resp.Header.Get("Content-Encoding"),
	})
	if resp.Body != nil {
		resp.Body = newHTTPBody(resp.Body, c, "upstream.response", attempt)
	}
	return resp, nil
}

type httpBody struct {
	io.ReadCloser
	mu            sync.Mutex
	stream        *Stream // nil unless upstream bodies are copied
	digest        hash.Hash
	bytes         int64
	capture       *Capture
	kind, attempt string
	eof, ended    bool
}

func newHTTPBody(body io.ReadCloser, c *Capture, kind, attempt string) *httpBody {
	b := &httpBody{ReadCloser: body, digest: sha256.New(), capture: c, kind: kind, attempt: attempt}
	if c.upstreamBodies {
		b.stream = NewStream(c, kind+".body")
		b.stream.metadata = map[string]any{"attempt_id": attempt}
	}
	return b
}

func (b *httpBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.ended {
		if n > 0 {
			b.digest.Write(p[:n])
			b.bytes += int64(n)
			b.stream.Write(p[:n])
		}
		if err == io.EOF {
			b.eof = true
			b.finish()
		}
	}
	return n, err
}

func (b *httpBody) Close() error {
	// Close the transport before taking the capture lock, so blocked reads
	// can be canceled without a telemetry-induced shutdown deadlock.
	err := b.ReadCloser.Close()
	b.mu.Lock()
	defer b.mu.Unlock()
	b.finish()
	return err
}

func (b *httpBody) finish() {
	if b.ended {
		return
	}
	b.stream.Flush()
	b.capture.Record(b.kind+".end", nil, map[string]any{"attempt_id": b.attempt, "body_complete": b.eof,
		"bytes": b.bytes, "sha256": hex.EncodeToString(b.digest.Sum(nil)), "body_copied": b.stream != nil})
	b.ended = true
}
