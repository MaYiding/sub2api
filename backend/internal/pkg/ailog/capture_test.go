package ailog

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type testSink struct {
	events []Event
	reject bool
}

func (s *testSink) Submit(e Event) bool { s.events = append(s.events, e); return !s.reject }

func TestCaptureLargeMessageAndConcurrentSequence(t *testing.T) {
	s := &testSink{}
	c := New(s, "source", "run", nil)
	data := bytes.Repeat([]byte("工具🙂"), 25000)
	c.Record("ws.client", data, nil)
	var result []byte
	messageID := s.events[1].MessageID
	for _, e := range s.events[1:] {
		if e.MessageID != messageID {
			t.Fatal("fragment lost message identity")
		}
		part, err := base64.StdEncoding.DecodeString(e.Data)
		if err != nil {
			t.Fatal(err)
		}
		result = append(result, part...)
	}
	if !bytes.Equal(result, data) {
		t.Fatal("large Unicode message changed")
	}
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); c.Record("ws.server", []byte("delta"), nil) }()
	}
	wg.Wait()
	c.Finish(nil)
	ids := map[string]bool{}
	for i, e := range s.events {
		if e.Sequence != uint64(i) || ids[e.EventID] {
			t.Fatal("unstable identity or out-of-order sequence")
		}
		ids[e.EventID] = true
	}
}

func TestCaptureReportsLossAndPreservesPartialStream(t *testing.T) {
	s := &testSink{}
	c := New(s, "source", "", nil)
	s.reject = true
	c.Record("ws.client", []byte("lost"), nil)
	s.reject = false
	stream := NewStream(c, "http.response.body")
	stream.Write([]byte("data: {\"partial\":true}\n\n"))
	stream.Flush()
	c.Finish(nil)
	end := s.events[len(s.events)-1]
	if end.Metadata["dropped_events"] != uint64(1) {
		t.Fatal("capture loss was hidden")
	}
	partial, err := base64.StdEncoding.DecodeString(s.events[len(s.events)-2].Data)
	if err != nil || string(partial) != "data: {\"partial\":true}\n\n" {
		t.Fatal("partial SSE lost")
	}
}

func TestWALClosePersistsAllAcknowledgedSubmissions(t *testing.T) {
	dir := t.TempDir()
	w, err := NewWAL(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		if !w.Submit(Event{SchemaVersion: 1, Sequence: uint64(i)}) {
			t.Fatal("unexpected drop")
		}
	}
	w.Close()
	files, err := filepath.Glob(filepath.Join(dir, "*.ready"))
	if err != nil || len(files) == 0 {
		t.Fatal("WAL not finalized")
	}
	count := 0
	for _, name := range files {
		info, err := os.Stat(name)
		if err != nil || info.Mode().Perm() != 0600 {
			t.Fatal("unsafe WAL permissions")
		}
		content, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range bytes.Split(bytes.TrimSpace(content), []byte("\n")) {
			var e Event
			if err := json.Unmarshal(line, &e); err != nil {
				t.Fatal(err)
			}
			if e.Sequence != uint64(count) {
				t.Fatal("WAL order changed")
			}
			count++
		}
	}
	if count != 20 {
		t.Fatalf("persisted %d of 20 records", count)
	}
}

func TestWALRejectsSymlinkDirectory(t *testing.T) {
	dir := t.TempDir()
	link := filepath.Join(dir, "link")
	if err := os.Symlink(t.TempDir(), link); err != nil {
		t.Fatal(err)
	}
	if w, err := NewWAL(link, 64<<20); err == nil {
		w.Close()
		t.Fatal("accepted symlink WAL")
	}
}

func TestWALReportsWriterLossInCaptureEnd(t *testing.T) {
	dir := t.TempDir()
	filler := filepath.Join(dir, "filler.ready")
	if err := os.WriteFile(filler, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Truncate(filler, 64<<20); err != nil {
		t.Fatal(err)
	}
	w, err := NewWAL(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	// Accepted into the queue, then lost by the writer: the spool is full.
	lost := New(w, "source", "", nil)
	lost.Record("http.request.body", []byte("lost"), nil)
	other := New(w, "source", "", nil)
	other.Record("http.request.body", []byte("lost too"), nil)
	for deadline := time.Now().Add(5 * time.Second); w.dropped.Load() < 4; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("writer did not process the full spool")
		}
	}
	if err := os.Remove(filler); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2500 * time.Millisecond) // the writer re-reads spool usage every second
	lost.Finish(nil)
	clean := New(w, "source", "", nil)
	clean.Finish(nil)
	w.Close()
	ends := map[string]Event{}
	files, _ := filepath.Glob(filepath.Join(dir, "*.ready"))
	for _, name := range files {
		content, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range bytes.Split(bytes.TrimSpace(content), []byte("\n")) {
			var e Event
			if err := json.Unmarshal(line, &e); err != nil {
				t.Fatal(err)
			}
			if e.Kind == "capture.end" {
				ends[e.CaptureID] = e
			}
		}
	}
	end, ok := ends[lost.id]
	if !ok || end.Metadata["wal_dropped_events"] != float64(2) || end.Metadata["dropped_events"] != float64(0) {
		t.Fatalf("writer loss not attributed: %v", end.Metadata)
	}
	if _, ok := ends[clean.id].Metadata["wal_dropped_events"]; ok {
		t.Fatal("clean capture reported loss")
	}
	if _, ok := ends[other.id]; ok {
		t.Fatal("unfinished capture has an end")
	}
}
