package budget

import (
	"encoding/json"
	"io"
	"sync"
)

// JSONLSink writes one JSON object per event line to an io.Writer. It is safe
// for concurrent use; writes are serialized with a mutex.
type JSONLSink struct {
	mu sync.Mutex
	w  io.Writer
}

// NewJSONLSink wraps w (e.g. a file or os.Stdout).
func NewJSONLSink(w io.Writer) *JSONLSink { return &JSONLSink{w: w} }

// Record implements Sink.
func (s *JSONLSink) Record(e Event) {
	b, err := json.Marshal(e)
	if err != nil {
		return
	}
	b = append(b, '\n')
	s.mu.Lock()
	_, _ = s.w.Write(b)
	s.mu.Unlock()
}
