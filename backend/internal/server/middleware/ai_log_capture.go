package middleware

import (
	"io"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ailog"
	"github.com/gin-gonic/gin"
)

// AILogCapture belongs after API authentication and before any body/model
// rewriting. Only the gateway route groups install this middleware.
func AILogCapture() gin.HandlerFunc {
	w, source := ailog.Default()
	if w == nil {
		return func(c *gin.Context) { c.Next() }
	}
	return aiLogCapture(w, source)
}

func aiLogCapture(sink ailog.Sink, source string) gin.HandlerFunc {
	return func(c *gin.Context) {
		isWS := strings.EqualFold(c.GetHeader("Upgrade"), "websocket")
		if c.Request.Method != http.MethodPost && !isWS {
			c.Next()
			return
		}
		metadata := map[string]any{
			"method": c.Request.Method, "route": c.FullPath(),
			"content_type":     c.GetHeader("Content-Type"),
			"content_encoding": c.GetHeader("Content-Encoding"),
			"protocol":         "http", "request_id": c.Writer.Header().Get("X-Request-ID"),
		}
		if isWS {
			metadata["protocol"] = "websocket"
		}
		if key, ok := GetAPIKeyFromContext(c); ok && key != nil {
			metadata["api_key_id"] = key.ID
			metadata["user_id"] = key.UserID
			if key.Group != nil {
				metadata["group_id"] = key.Group.ID
				metadata["platform"] = key.Group.Platform
			}
		}
		// Do not copy arbitrary headers, URL query strings, Authorization or
		// cookies. Correlation values are bounded opaque identifiers.
		trace := ""
		for _, header := range []string{"X-Agent-Run-ID", "X-Trace-ID", "Thread-ID", "X-Session-ID", "Session_id", "Session-ID"} {
			if id := aiLogCorrelation(c.GetHeader(header)); id != "" {
				metadata[strings.ToLower(header)] = id
				if trace == "" {
					trace = id
				}
			}
		}
		for _, header := range []string{"Traceparent", "X-Agent-Span-ID", "X-Parent-Span-ID", "X-Codex-Window-ID", "X-OpenAI-Subagent"} {
			if id := aiLogCorrelation(c.GetHeader(header)); id != "" {
				metadata[strings.ToLower(header)] = id
			}
		}
		if trace == "" {
			parts := strings.Split(c.GetHeader("Traceparent"), "-")
			if len(parts) == 4 && len(parts[1]) == 32 {
				trace = aiLogCorrelation(parts[1])
			}
		}
		capture := ailog.New(sink, source, trace, metadata)
		c.Request = c.Request.WithContext(ailog.WithContext(c.Request.Context(), capture))
		requestStream := ailog.NewStream(capture, "http.request.body")
		var body *aiLogBody
		if c.Request.Body != nil && !isWS {
			body = &aiLogBody{ReadCloser: c.Request.Body, stream: requestStream}
			c.Request.Body = body
		}
		writer := &aiLogWriter{ResponseWriter: c.Writer, stream: ailog.NewStream(capture, "http.response.body")}
		c.Writer = writer
		completed := false
		defer func() {
			requestStream.Flush()
			writer.stream.Flush()
			capture.Finish(map[string]any{
				"status": writer.Status(), "response_bytes": writer.Size(),
				"response_content_type": writer.Header().Get("Content-Type"),
				"request_body_complete": body == nil || body.eof,
				"response_write_failed": writer.failed,
				"handler_completed":     completed,
				"client_canceled":       c.Request.Context().Err() != nil,
			})
		}()
		c.Next()
		completed = true
	}
}

func aiLogCorrelation(value string) string {
	if len(value) == 0 || len(value) > 128 {
		return ""
	}
	for _, r := range value {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("-_.:", r)) {
			return ""
		}
	}
	return value
}

type aiLogBody struct {
	io.ReadCloser
	stream *ailog.Stream
	eof    bool
}

func (b *aiLogBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if n > 0 {
		b.stream.Write(p[:n])
	}
	if err == io.EOF {
		b.eof = true
	}
	return n, err
}

type aiLogWriter struct {
	gin.ResponseWriter
	stream *ailog.Stream
	failed bool
}

func (w *aiLogWriter) Write(p []byte) (int, error) {
	n, err := w.ResponseWriter.Write(p)
	if n > 0 {
		w.stream.Write(p[:n])
	}
	if err != nil {
		w.failed = true
	}
	return n, err
}

func (w *aiLogWriter) WriteString(s string) (int, error) { return w.Write([]byte(s)) }

// Unwrap preserves ResponseController access to the original writer.
func (w *aiLogWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
