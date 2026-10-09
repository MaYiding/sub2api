package ailog

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
)

const maxQueuedBytes = 32 << 20

type WAL struct {
	dir       string
	maxBytes  int64
	queue     chan []byte
	queued    atomic.Int64
	dropped   atomic.Uint64
	stop      chan struct{}
	done      chan struct{}
	closeOnce sync.Once
	closed    atomic.Bool
	submitMu  sync.RWMutex
}

var defaultOnce sync.Once
var defaultWAL *WAL
var defaultSource string

// Default is disabled unless explicitly enabled. An invalid enabled
// configuration fails startup instead of silently running without capture.
func Default() (*WAL, string) {
	defaultOnce.Do(func() {
		if os.Getenv("AI_LOG_ENABLED") != "true" {
			return
		}
		dir := os.Getenv("AI_LOG_SPOOL_DIR")
		defaultSource = strings.TrimSpace(os.Getenv("AI_LOG_SOURCE_ID"))
		if dir == "" || !filepath.IsAbs(dir) || defaultSource == "" || len(defaultSource) > 128 {
			panic("AI_LOG_ENABLED requires an absolute AI_LOG_SPOOL_DIR and AI_LOG_SOURCE_ID (1..128 bytes)")
		}
		maxBytes := int64(20 << 30)
		if value := os.Getenv("AI_LOG_SPOOL_MAX_BYTES"); value != "" {
			var err error
			maxBytes, err = strconv.ParseInt(value, 10, 64)
			if err != nil || maxBytes < 64<<20 {
				panic("AI_LOG_SPOOL_MAX_BYTES must be at least 67108864")
			}
		}
		var err error
		defaultWAL, err = NewWAL(dir, maxBytes)
		if err != nil {
			panic(fmt.Errorf("initialize AI log WAL: %w", err))
		}
	})
	return defaultWAL, defaultSource
}

func NewWAL(dir string, maxBytes int64) (*WAL, error) {
	if maxBytes < 64<<20 {
		return nil, errors.New("WAL limit must be at least 64 MiB")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("WAL path must be a real directory")
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	w := &WAL{dir: dir, maxBytes: maxBytes, queue: make(chan []byte, 1024), stop: make(chan struct{}), done: make(chan struct{})}
	// Verify write access synchronously, before accepting requests.
	f, err := os.CreateTemp(dir, ".write-check-")
	if err != nil {
		return nil, err
	}
	name := f.Name()
	if err := lockFile(f); err != nil {
		_ = f.Close()
		_ = os.Remove(name)
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	if err := os.Remove(name); err != nil {
		return nil, err
	}
	go w.run()
	return w, nil
}

func (w *WAL) Submit(e Event) bool {
	if w == nil {
		return false
	}
	w.submitMu.RLock()
	defer w.submitMu.RUnlock()
	if w.closed.Load() {
		return false
	}
	b, err := json.Marshal(e)
	if err != nil {
		w.dropped.Add(1)
		return false
	}
	b = append(b, '\n')
	size := int64(len(b))
	if w.queued.Add(size) > maxQueuedBytes {
		w.queued.Add(-size)
		w.dropped.Add(1)
		return false
	}
	select {
	case <-w.stop:
	case w.queue <- b:
		return true
	default:
	}
	w.queued.Add(-size)
	w.dropped.Add(1)
	return false
}

func (w *WAL) Close() {
	w.closeOnce.Do(func() {
		w.submitMu.Lock()
		w.closed.Store(true)
		close(w.stop)
		w.submitMu.Unlock()
	})
	<-w.done
}

// Shutdown drains the default WAL after the HTTP server and app services stop.
func Shutdown() {
	if defaultWAL != nil {
		defaultWAL.Close()
	}
}

func (w *WAL) diskUsage() (int64, error) {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return 0, err
	}
	var total int64
	for _, entry := range entries {
		if entry.IsDir() || !(strings.HasSuffix(entry.Name(), ".open") || strings.HasSuffix(entry.Name(), ".ready")) {
			continue
		}
		info, err := entry.Info()
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return 0, err
		}
		total += info.Size()
	}
	return total, nil
}

func (w *WAL) run() {
	defer close(w.done)
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var f *os.File
	var segmentBytes int64
	usage, usageErr := w.diskUsage()
	var lastReport time.Time
	report := func(err error) {
		if time.Since(lastReport) >= 30*time.Second {
			slog.Error("ai_log_capture_degraded", "dropped_events", w.dropped.Load(), "error", err)
			lastReport = time.Now()
		}
	}
	rotate := func() {
		if f == nil {
			return
		}
		name := f.Name()
		err := f.Sync()
		if err == nil {
			err = os.Rename(name, strings.TrimSuffix(name, ".open")+".ready")
		}
		if closeErr := f.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			report(err)
		}
		f = nil
		segmentBytes = 0
	}
	defer rotate()
	write := func(b []byte) {
		defer w.queued.Add(-int64(len(b)))
		if usageErr != nil || usage+int64(len(b)) > w.maxBytes {
			w.dropped.Add(1)
			report(errors.New("AI log spool capacity exceeded or unavailable"))
			return
		}
		if f == nil {
			var err error
			name := filepath.Join(w.dir, time.Now().UTC().Format("20060102T150405.000000000")+"-"+uuid.NewString()+".open")
			f, err = os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if err == nil {
				err = lockFile(f)
			}
			if err != nil {
				if f != nil {
					_ = f.Close()
					f = nil
				}
				w.dropped.Add(1)
				report(err)
				return
			}
		}
		n, err := f.Write(b)
		usage += int64(n)
		segmentBytes += int64(n)
		if err != nil {
			w.dropped.Add(1)
			report(err)
			// Remove a partial JSON line so the following event stays parseable.
			if truncErr := f.Truncate(segmentBytes - int64(n)); truncErr != nil {
				report(truncErr)
			}
			rotate()
		}
		if segmentBytes >= 4<<20 {
			rotate()
		}
	}
	for {
		select {
		case b := <-w.queue:
			write(b)
		case <-ticker.C:
			rotate()
			usage, usageErr = w.diskUsage()
			if w.dropped.Load() > 0 {
				report(errors.New("capture loss counter is nonzero; inspect source spool and shipper"))
			}
		case <-w.stop:
			for {
				select {
				case b := <-w.queue:
					write(b)
				default:
					return
				}
			}
		}
	}
}
