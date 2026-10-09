package service

import (
	"context"

	"github.com/Wei-Shaw/sub2api/internal/pkg/ailog"
	coderws "github.com/coder/websocket"
)

// WriteOpenAIWSClientMessage records the application frame after a successful
// downstream write. It preserves the caller's cancellation and timeout.
func WriteOpenAIWSClientMessage(ctx context.Context, conn *coderws.Conn, kind coderws.MessageType, payload []byte) error {
	err := conn.Write(ctx, kind, payload)
	if err == nil {
		ailog.FromContext(ctx).Record("ws.server", payload, map[string]any{"message_type": int(kind)})
	} else {
		ailog.FromContext(ctx).Record("ws.write_failed", nil, nil)
	}
	return err
}
