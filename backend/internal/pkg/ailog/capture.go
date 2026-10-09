// Package ailog records opt-in gateway traffic to a bounded local WAL. Network
// delivery is owned by the companion shipper, never by an inference request.
package ailog

import (
	"context"
	"encoding/base64"
	"sync"
	"time"

	"github.com/google/uuid"
)

const ChunkBytes = 64 << 10

type Event struct {
	SchemaVersion int            `json:"schema_version"`
	EventID       string         `json:"event_id"`
	SourceID      string         `json:"source_id"`
	CaptureID     string         `json:"capture_id"`
	TraceID       string         `json:"trace_id"`
	Sequence      uint64         `json:"sequence"`
	Timestamp     string         `json:"timestamp"`
	Kind          string         `json:"kind"`
	MessageID     string         `json:"message_id,omitempty"`
	Part          int            `json:"part,omitempty"`
	Parts         int            `json:"parts,omitempty"`
	Data          string         `json:"data_base64,omitempty"`
	Metadata      map[string]any `json:"metadata,omitempty"`
}

type Sink interface{ Submit(Event) bool }

type Capture struct {
	mu                sync.Mutex
	sink              Sink
	source, id, trace string
	seq, dropped      uint64
	finished          bool
}

type contextKey struct{}

func New(sink Sink, source, trace string, metadata map[string]any) *Capture {
	if sink == nil {
		return nil
	}
	id := uuid.NewString()
	if trace == "" {
		trace = id
	}
	c := &Capture{sink: sink, source: source, id: id, trace: trace}
	c.emit("capture.start", "", 0, 0, nil, metadata)
	return c
}

func WithContext(ctx context.Context, c *Capture) context.Context {
	return context.WithValue(ctx, contextKey{}, c)
}

func FromContext(ctx context.Context) *Capture {
	if ctx == nil {
		return nil
	}
	c, _ := ctx.Value(contextKey{}).(*Capture)
	return c
}

// Record preserves message boundaries and splits large messages before they
// reach Kafka. A stable event ID survives all shipper retries.
func (c *Capture) Record(kind string, data []byte, metadata map[string]any) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.finished {
		return
	}
	message := uuid.NewString()
	parts := (len(data) + ChunkBytes - 1) / ChunkBytes
	if parts == 0 {
		parts = 1
	}
	for part := 0; part < parts; part++ {
		start, end := part*ChunkBytes, (part+1)*ChunkBytes
		if end > len(data) {
			end = len(data)
		}
		c.emit(kind, message, part, parts, data[start:end], metadata)
	}
}

func (c *Capture) Finish(metadata map[string]any) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.finished {
		return
	}
	if metadata == nil {
		metadata = map[string]any{}
	}
	metadata["dropped_events"] = c.dropped
	metadata["prior_events"] = c.seq
	c.emit("capture.end", "", 0, 0, nil, metadata)
	c.finished = true
}

func (c *Capture) emit(kind, message string, part, parts int, data []byte, metadata map[string]any) {
	e := Event{SchemaVersion: 1, EventID: uuid.NewString(), SourceID: c.source,
		CaptureID: c.id, TraceID: c.trace, Sequence: c.seq,
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano), Kind: kind,
		MessageID: message, Part: part, Parts: parts, Metadata: metadata}
	if len(data) > 0 {
		e.Data = base64.StdEncoding.EncodeToString(data)
	}
	c.seq++
	if !c.sink.Submit(e) {
		c.dropped++
	}
}

// Stream coalesces HTTP writes independently of SSE Flush calls. It never
// delays the bytes sent to the client; only telemetry is buffered.
type Stream struct {
	capture  *Capture
	kind     string
	buffer   []byte
	metadata map[string]any
}

func NewStream(c *Capture, kind string) *Stream {
	return &Stream{capture: c, kind: kind}
}

func (s *Stream) Write(data []byte) {
	if s == nil || s.capture == nil {
		return
	}
	for len(data) > 0 {
		n := ChunkBytes - len(s.buffer)
		if n > len(data) {
			n = len(data)
		}
		s.buffer = append(s.buffer, data[:n]...)
		data = data[n:]
		if len(s.buffer) == ChunkBytes {
			s.Flush()
		}
	}
}

func (s *Stream) Flush() {
	if s == nil || len(s.buffer) == 0 {
		return
	}
	s.capture.Record(s.kind, s.buffer, s.metadata)
	s.buffer = s.buffer[:0]
}
