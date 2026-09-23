package sim

import (
	"fmt"

	"chmig/ring"
)

const loaderID = "client:__loader__"

// loader is a special client that seeds one record per key at tick 0 when
// Config.Preload is set. It behaves like a normal write client (same redirect
// and retry semantics) but its writes are issued immediately and it needs no
// workload plan.
type loader struct {
	aid     string
	eng     *Engine
	w       *world
	current *ring.Ring
	pending *ring.Ring

	pending_ map[string]*inflightLoader
}

type inflightLoader struct {
	key, reqID, value string
	to                string
	attempts          int
	tid               int64
}

func newLoader(w *world) *loader {
	l := &loader{
		aid: loaderID, eng: w.eng, w: w,
		current: w.rings[0], pending_: map[string]*inflightLoader{},
	}
	for _, key := range w.cfg.Keys {
		reqID := fmt.Sprintf("preload:%s", key)
		inf := &inflightLoader{
			key: key, reqID: reqID, value: "seed:" + key,
			to: nodeID(w.rings[0].Owner(key)),
		}
		l.pending_[reqID] = inf
		w.eng.after(0, l, "issue", reqID)
	}
	return l
}

func (l *loader) id() string { return l.aid }

func (l *loader) receive(m *Msg) {
	switch b := m.Body.(type) {
	case *TopologyAnnounce:
		l.eng.send(&Msg{Type: "TopologyAck", From: l.aid, To: m.From, Seq: m.Seq, Body: &TopologyAck{Version: b.Version}})
		r, err := ring.New(b.Version, l.w.nodeSpecs(b.Nodes), l.w.cfg.VNodes)
		if err != nil {
			panic(err)
		}
		if b.Pending {
			l.pending = r
		} else if l.current.Version < b.Version {
			l.current = r
			l.pending = nil
		}
	case *CommitAnnounce:
		l.eng.send(&Msg{Type: "CommitAck", From: l.aid, To: m.From, Seq: m.Seq, Body: &CommitAck{Version: b.Version}})
		if l.pending != nil && l.pending.Version == b.Version {
			l.current = l.pending
			l.pending = nil
		}
	case *WriteAck:
		inf := l.pending_[b.ReqID]
		if inf == nil {
			return
		}
		if b.Redirect != "" {
			l.eng.cancelTimer(inf.tid)
			inf.to = b.Redirect
			inf.attempts++
			l.send(inf)
			inf.tid = l.eng.after(int64(l.w.cfg.RetryTicks), l, "retry", inf.reqID)
			return
		}
		l.eng.cancelTimer(inf.tid)
		delete(l.pending_, b.ReqID)
		l.w.stats.WritesConfirmed++
		l.w.ledger.noteConfirm(b.ReqID, ConfirmedWrite{
			Key: b.Key, Ver: b.Ver, Value: b.Value, ConfirmedTick: l.eng.tick(),
		})
	}
}

func (l *loader) timer(kind string, data any) {
	reqID := data.(string)
	inf := l.pending_[reqID]
	if inf == nil {
		return
	}
	if kind == "issue" {
		l.w.stats.WritesIssued++
		l.send(inf)
		inf.tid = l.eng.after(int64(l.w.cfg.RetryTicks), l, "retry", reqID)
		return
	}
	inf.attempts++
	if inf.attempts > l.w.maxClientAttempts() {
		delete(l.pending_, reqID)
		l.w.stats.WritesFailed++
		return
	}
	l.send(inf)
	inf.tid = l.eng.after(int64(l.w.cfg.RetryTicks), l, "retry", reqID)
}

func (l *loader) send(inf *inflightLoader) {
	l.eng.send(&Msg{Type: "WriteReq", From: l.aid, To: inf.to, Seq: l.w.newSeq(),
		Body: &WriteReq{Key: inf.key, Value: inf.value, ReqID: inf.reqID}})
}
