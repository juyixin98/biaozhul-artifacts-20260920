package sim

import "sort"

// Record is one applied version of a key.
type Record struct {
	Ver    int
	Value  string
	ReqID  string
	AtTick int64
	Node   string
}

// ConfirmedWrite records a write that received a client ack.
type ConfirmedWrite struct {
	Key           string
	Ver           int
	Value         string
	ConfirmedTick int64
}

// ReadObservation is one client read, validated after the run.
type ReadObservation struct {
	Client    string
	Key       string
	IssueTick int64
	RespTick  int64
	Ver       int
	Value     string
	Found     bool
	Success   bool
}

// Ledger is the simulator's external ground truth. It observes every local
// apply, every ack and every read answer; the verification pass uses it to
// judge the storage nodes, never the reverse — storage code cannot "win" an
// argument by writing here.
type Ledger struct {
	// applied versions per key, keyed by version.
	applied   map[string]map[int]Record
	latest    map[string]Record
	confirmed map[string]ConfirmedWrite // reqID -> write
	reads     []ReadObservation
}

func newLedger() *Ledger {
	return &Ledger{
		applied:   map[string]map[int]Record{},
		latest:    map[string]Record{},
		confirmed: map[string]ConfirmedWrite{},
	}
}

// applied is called whenever a node commits a write to its local store.
func (l *Ledger) noteApply(key, reqID string, ver int, value string, node string, tick int64) {
	if l.applied[key] == nil {
		l.applied[key] = map[int]Record{}
	}
	rec := Record{Ver: ver, Value: value, ReqID: reqID, AtTick: tick, Node: node}
	l.applied[key][ver] = rec
	if cur, ok := l.latest[key]; !ok || ver > cur.Ver {
		l.latest[key] = rec
	}
}

func (l *Ledger) noteConfirm(reqID string, cw ConfirmedWrite) {
	if _, ok := l.confirmed[reqID]; ok {
		return // duplicate ack delivery must not double-count
	}
	l.confirmed[reqID] = cw
}

func (l *Ledger) noteRead(r ReadObservation) {
	// Deduplicate identical observations: the same read can resolve from more
	// than one timer firing at the same tick. Read counts are small, so a full
	// scan keeps the rule obviously correct.
	for _, p := range l.reads {
		if p.Client == r.Client && p.Key == r.Key &&
			p.IssueTick == r.IssueTick && p.RespTick == r.RespTick {
			return
		}
	}
	l.reads = append(l.reads, r)
}

func (l *Ledger) latestRecord(key string) (Record, bool) {
	r, ok := l.latest[key]
	return r, ok
}

// confirmedAt returns the highest version whose ack was observed no later than
// tick — the lower bound a read answer at that time must meet.
func (l *Ledger) confirmedAt(key string, tick int64) int {
	best := 0
	for _, c := range l.confirmed {
		if c.Key != key || c.ConfirmedTick > tick {
			continue
		}
		if c.Ver > best {
			best = c.Ver
		}
	}
	return best
}

// confirmedWrites returns confirmed writes sorted deterministically.
func (l *Ledger) confirmedList() []ConfirmedWrite {
	out := make([]ConfirmedWrite, 0, len(l.confirmed))
	for _, c := range l.confirmed {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Key != out[j].Key {
			return out[i].Key < out[j].Key
		}
		return out[i].Ver < out[j].Ver
	})
	return out
}
