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
