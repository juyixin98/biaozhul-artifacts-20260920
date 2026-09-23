package sim

// Msg is the envelope exchanged by actors through the lossy network.
type Msg struct {
	Type string
	From string
	To   string
	// Seq uniquely identifies a logical send; retransmissions reuse the same
	// Seq so receivers can deduplicate.
	Seq int64
	// Migration traffic flag: the simulated interruption blackholes messages
	// with Migration=true; client and control traffic keeps flowing.
	Migration bool
	Body      interface{}
}

// Payload size in (simulated) bytes. Record payloads use the value length;
// control messages use a small fixed header cost.
func (m *Msg) Size() int {
	switch b := m.Body.(type) {
	case *WriteReq:
		return 32 + len(b.Value)
	case *WriteAck:
		return 40
	case *ReadReq:
		return 16
	case *ReadResp:
		if b.Found {
			return 32 + len(b.Value)
		}
		return 24
	case *MigBegin:
		return 24
	case *MigSnapshot:
		return 32 + len(b.Value)
	case *MigForward:
		return 40 + len(b.Value)
	case *MigAck:
		return 24
	case *MigBarrier:
		return 28
	case *MigBarrierAck:
		return 24
	case *StartMigration:
		return 24
	case *StartBarrier:
		return 24
	case *BarrierDone:
		return 24
	case *TopologyAnnounce:
		return 64 + len(b.Nodes)*16
	case *TopologyAck:
		return 24
	case *CommitAnnounce:
		return 32
	case *CommitAck:
		return 24
	case *Decommission:
		return 16
	}
	return 16
}

// ---- client <-> node: writes -------------------------------------------------

// WriteReq carries a client write. ReqID is globally unique; nodes apply an
// idempotency table on it, so retries or duplicate delivery never apply twice.
type WriteReq struct {
	Key   string
	Value string
	ReqID string
}

// WriteAck is the single-version decision. Ver is the key's monotonically
// increasing version assigned by the current coordinator.
type WriteAck struct {
	Key      string
	ReqID    string
	Ver      int
	Value    string
	Redirect string // non-empty: write was rejected pre-barrier, retry on node
}

// ---- client <-> node: reads --------------------------------------------------

type ReadReq struct {
	RID string
	Key string
}

// ReadResp is an individual node's answer. During dual-read the client takes
// the answer with the greatest Ver — the single-version arbiter.
type ReadResp struct {
	RID   string
	Key   string
	Found bool
	Ver   int
	Value string
}

// ---- node A (old owner) <-> node B (new owner) -------------------------------

// MigBegin opens the per-key migration stream.
type MigBegin struct {
	Key     string
	RingVer int
	NextVer int // expected per-key apply seq of the first forwarded write
}

// MigSnapshot ships the record visible on A at the moment migration starts,
// together with A's idempotency table for the key, so B can deduplicate client
// retries that arrive after the switch.
type MigSnapshot struct {
	Key         string
	RingVer     int
	Ver         int
	Value       string
	Idempotents map[string]int // reqID -> applied version
	NextVer     int
}

// MigForward streams every write A applies after the snapshot, in per-key
// sequence order. SeqNo is the key's apply sequence (snapshot covers < SeqNo).
type MigForward struct {
	Key     string
	RingVer int
	SeqNo   int
	ReqID   string
	Ver     int
	Value   string
}

// MigAck acknowledges snapshot (SeqNo == snapshot version) or forwards.
type MigAck struct {
	Key     string
	RingVer int
	SeqNo   int
}

// MigBarrier is the switch barrier: once B acknowledges it, having applied the
// snapshot and every forwarded write, the key is safe to serve from B.
// UpTo is the per-key stream sequence of the last write A will forward; B only
// completes the barrier after applying through UpTo contiguously.
type MigBarrier struct {
	Key     string
	RingVer int
	UpTo    int
}

type MigBarrierAck struct {
	Key     string
	RingVer int
}

// ---- controller -> nodes: migration drive ------------------------------------

// StartMigration tells the old owner to open the per-key migration stream.
type StartMigration struct {
	Key     string
	RingVer int
}

// StartBarrier tells the old owner to freeze and issue the switch barrier.
type StartBarrier struct {
	Key     string
	RingVer int
}

// BarrierDone is the new (or old) owner's report that a key's barrier passed.
type BarrierDone struct {
	Key     string
	RingVer int
}

// DecommissionAck confirms a removed node has shut down.
type DecommissionAck struct {
	Version int
}

// ---- controller <-> nodes/clients -------------------------------------------

// TopologyAnnounce reliably publishes a pending or committed ring.
type TopologyAnnounce struct {
	Version int
	Nodes   []string
	Pending bool // true: migration in progress, dual-read against old+new
}

type TopologyAck struct {
	Version int
}

// CommitAnnounce finalizes a ring epoch after every key barrier passed.
type CommitAnnounce struct {
	Version int
}

type CommitAck struct {
	Version int
}

// Decommission tells a removed physical node to shut down. It is only sent
// after the commit gate proves no latest record is left on the node.
type Decommission struct {
	Version int
}
