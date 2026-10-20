//go:build unit

package middleware

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ailog"
	"github.com/gin-gonic/gin"
)

type aiLogTestSink struct{ events []ailog.Event }

func (s *aiLogTestSink) Submit(e ailog.Event) bool { s.events = append(s.events, e); return true }

func TestAILogCapturePreservesSSEAndExcludesCredentials(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := &aiLogTestSink{}
	router := gin.New()
	router.Use(aiLogCapture(s, "test"))
	requestBody := `{"model":"test","messages":[{"role":"user","content":"你好"}]}`
	responseBody := "data: {\"delta\":\"好\"}\n\ndata: [DONE]\n\n"
	router.POST("/v1/messages", func(c *gin.Context) {
		body, err := io.ReadAll(c.Request.Body)
		if err != nil || string(body) != requestBody {
			t.Error("request changed")
		}
		c.Header("Content-Type", "text/event-stream")
		if _, err := c.Writer.WriteString(responseBody); err != nil {
			t.Error(err)
		}
		c.Writer.Flush()
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/messages?key=QUERY_SECRET", strings.NewReader(requestBody))
	req.Header.Set("Authorization", "Bearer AUTH_SECRET")
	req.Header.Set("Cookie", "COOKIE_SECRET")
	req.Header.Set("X-Agent-Run-ID", "run-123")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, req)
	if response.Body.String() != responseBody || !response.Flushed {
		t.Fatal("SSE delivery changed")
	}
	bodies := map[string]string{}
	for _, event := range s.events {
		data, err := base64.StdEncoding.DecodeString(event.Data)
		if err != nil {
			t.Fatal(err)
		}
		bodies[event.Kind] += string(data)
		if event.TraceID != "run-123" {
			t.Fatal("run correlation lost")
		}
	}
	if bodies["http.request.body"] != requestBody || bodies["http.response.body"] != responseBody {
		t.Fatal("captured bytes changed")
	}
	serialized, err := json.Marshal(s.events)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"AUTH_SECRET", "QUERY_SECRET", "COOKIE_SECRET"} {
		if strings.Contains(string(serialized), secret) {
			t.Fatal("request credential captured")
		}
	}
}

func TestAILogCaptureMarksUnreadRequestBody(t *testing.T) {
	s := &aiLogTestSink{}
	router := gin.New()
	router.Use(aiLogCapture(s, "test"))
	router.POST("/v1/messages", func(c *gin.Context) { c.Status(http.StatusForbidden) })
	router.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/messages", strings.NewReader("unread")))
	end := s.events[len(s.events)-1]
	if end.Metadata["request_body_complete"] != false {
		t.Fatal("unread body reported complete")
	}
}

func TestAILogCapturePrefersClaudeCodeSession(t *testing.T) {
	s := &aiLogTestSink{}
	router := gin.New()
	router.Use(aiLogCapture(s, "test"))
	router.POST("/v1/messages", func(c *gin.Context) { c.Status(http.StatusOK) })
	req := httptest.NewRequest(http.MethodPost, "/v1/messages", strings.NewReader("{}"))
	req.Header.Set("X-Session-ID", "other")
	req.Header.Set("X-Claude-Code-Session-Id", "0f8e6a52-5c1d-4a43-9d7e-1b2c3d4e5f60")
	req.Header.Set("X-Claude-Code-Agent-Id", "agent-2")
	req.Header.Set("X-Claude-Code-Parent-Agent-Id", "agent-1")
	router.ServeHTTP(httptest.NewRecorder(), req)
	start := s.events[0]
	if start.TraceID != "0f8e6a52-5c1d-4a43-9d7e-1b2c3d4e5f60" || start.Metadata["x-claude-code-session-id"] != start.TraceID {
		t.Fatal("Claude Code session not used as trace")
	}
	if start.Metadata["x-claude-code-agent-id"] != "agent-2" || start.Metadata["x-claude-code-parent-agent-id"] != "agent-1" || start.Metadata["x-session-id"] != "other" {
		t.Fatal("agent correlation lost")
	}
	if start.Metadata["upstream_bodies"] != false {
		t.Fatal("upstream body mode not recorded")
	}
}
