package sim

// TraceEvent is one row of the event log. Details are a generic map because
// every node emits its own fields; rows are serialized directly to JSON.
type TraceEvent struct {
	Seq    int64          `json:"seq"`
	Time   Time           `json:"time_ms"`
	Node   string         `json:"node,omitempty"`
	Event  string         `json:"event"`
	Detail map[string]any `json:"detail,omitempty"`
}

// Trace is a Recorder that retains every event in simulation order.
type Trace struct {
	Events []TraceEvent `json:"events"`
}

func NewTrace() *Trace { return &Trace{Events: []TraceEvent{}} }

func (t *Trace) Record(time Time, node, event string, detail map[string]any) {
	cp := make(map[string]any, len(detail))
	for k, v := range detail {
		cp[k] = v
	}
	t.Events = append(t.Events, TraceEvent{
		Seq: int64(len(t.Events)) + 1, Time: time, Node: node, Event: event, Detail: cp,
	})
}

// Select returns events matching predicate, keeping order.
func (t *Trace) Select(pred func(TraceEvent) bool) []TraceEvent {
	var out []TraceEvent
	for _, e := range t.Events {
		if pred(e) {
			out = append(out, e)
		}
	}
	return out
}
