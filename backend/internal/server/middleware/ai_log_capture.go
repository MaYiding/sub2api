package middleware

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ailog"
	"github.com/Wei-Shaw/sub2api/internal/service"
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
			addAIAPIKeyMetadata(metadata, key, includeAIAPIKey(sink))
		}
		// Do not copy arbitrary headers, URL query strings, Authorization or
		// cookies. Correlation values are bounded opaque identifiers. Claude
		// Code's own session id names the conversation, so it wins as trace.
		trace := ""
		for _, header := range []string{"X-Claude-Code-Session-Id", "X-Agent-Run-ID", "X-Trace-ID", "Thread-ID", "X-Session-ID", "Session_id", "Session-ID"} {
			if id := aiLogCorrelation(c.GetHeader(header)); id != "" {
				metadata[strings.ToLower(header)] = id
				if trace == "" {
					trace = id
				}
			}
		}
		for _, header := range []string{"Traceparent", "X-Agent-Span-ID", "X-Parent-Span-ID", "X-Codex-Window-ID", "X-OpenAI-Subagent",
			"X-Claude-Code-Agent-Id", "X-Claude-Code-Parent-Agent-Id"} {
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
		original := c.Writer
		writer := &aiLogWriter{ResponseWriter: original, stream: ailog.NewStream(capture, "http.response.body")}
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
			// Hand back the incoming writer. An outer wrapper (the ops error
			// logger) restores and releases its pooled writer only if it sees its
			// own, and the access log reads Status() after that release.
			c.Writer = original
		}()
		c.Next()
		completed = true
	}
}

// addAIAPIKeyMetadata records authenticated key identifiers. The full
// credential is added only when the sink explicitly opts in through
// AI_LOG_INCLUDE_API_KEYS. That opt-in is intended for the restricted
// internal deployment; callers must protect Kafka, ClickHouse and archive
// access as credential-bearing data stores.
func includeAIAPIKey(sink ailog.Sink) bool {
	provider, ok := sink.(interface{ IncludeAPIKeys() bool })
	return ok && provider.IncludeAPIKeys()
}

func addAIAPIKeyMetadata(metadata map[string]any, key *service.APIKey, includeKey bool) {
	if metadata == nil || key == nil {
		return
	}
	metadata["api_key_id"] = key.ID
	metadata["user_id"] = key.UserID
	if key.Name != "" {
		metadata["api_key_name"] = key.Name
	}
	if key.Key != "" && includeKey {
		metadata["api_key_capture"] = true
		metadata["api_key"] = key.Key
		metadata["api_key_prefix"] = aiAPIKeyPrefix(key.Key, 8)
		metadata["api_key_last4"] = aiAPIKeySuffix(key.Key, 4)
		digest := sha256.Sum256([]byte(key.Key))
		metadata["api_key_fingerprint"] = hex.EncodeToString(digest[:])
	}
	if key.Group != nil {
		metadata["group_id"] = key.Group.ID
		metadata["platform"] = key.Group.Platform
	}
}

func aiAPIKeyPrefix(value string, length int) string {
	if length <= 0 || len(value) <= length {
		return value
	}
	return value[:length]
}

func aiAPIKeySuffix(value string, length int) string {
	if length <= 0 || len(value) <= length {
		return value
	}
	return value[len(value)-length:]
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
