package sim

// Commit is one accepted write at the resource.
type Commit struct {
	At     Time   `json:"at"`
	Client string `json:"client"`
	Fence  int64  `json:"fence"`
	ReqID  string `json:"req_id"`
	Value  string `json:"value"`
}

// RejectedSubmit records every refused write. The reject reason is the
// observable proof that the fencing protocol stopped a stale holder.
type RejectedSubmit struct {
	At         Time   `json:"at"`
	Client     string `json:"client"`
	Fence      int64  `json:"fence"`
	ReqID      string `json:"req_id"`
	Reason     string `json:"reason"`
	EpochFence int64  `json:"epoch_fence"`
}

// ResourceSnapshot is the post-run state of the protected resource.
type ResourceSnapshot struct {
	NodeID      string           `json:"node"`
	Committed   []Commit         `json:"commits"`
	Rejected    []RejectedSubmit `json:"rejected"`
	MaxFence    int64            `json:"max_fence"`
	HolderFence int64            `json:"holder_fence"`
}

// Resource is a storage node that only applies writes tagged with the
// highest fence it has ever seen. The lock service may grant a new lease
// without the old holder knowing; this check is the second line of defense,
// and is what makes a zombie holder harmless.
type Resource struct {
	committed []Commit
	rejected  []RejectedSubmit
	maxFence  int64
	// seen dedupes retransmitted / network-duplicated submit requests, so a
	// legitimate retry cannot double-apply.
	seen map[string]submitSeen
}

type submitSeen struct {
	result string
	fence  int64
}

func NewResource() *Resource {
	return &Resource{
		committed: []Commit{},
		rejected:  []RejectedSubmit{},
		seen:      map[string]submitSeen{},
	}
}

func (r *Resource) ID() string { return ResourceID }

func (r *Resource) HandleMessage(h Host, t Time, msg *Envelope) {
	if msg.Type != TypeSubmitReq {
		h.Log("resource.unexpected", map[string]any{"type": msg.Type})
		return
	}

	ack := &Envelope{Src: ResourceID, Dst: msg.Src, ReqType: ReqSubmit, ReqID: msg.ReqID}

	// Re-delivery of a request already accepted with the same fence:
	// idempotent success, not a second commit and not a stale write. Check
	// this before the fence comparison, since an even newer holder may have
	// committed in between the original and its duplicate.
	if prev, ok := r.seen[msg.ReqID]; ok {
		ack.Type = TypeSubmitAck
		ack.Result = ResultDuplicate
		ack.Fence = prev.fence
		h.Log("resource.submit.dedup", map[string]any{
			"client": msg.Src, "req_id": msg.ReqID, "fence": msg.Fence,
		})
		h.Send(ack)
		return
	}

	reason := r.check(msg)

	if reason != "" {
		ack.Type = TypeSubmitAck
		ack.Result = reason
		ack.Reason = reason
		ack.Fence = r.maxFence
		r.rejected = append(r.rejected, RejectedSubmit{
			At: t, Client: msg.Src, Fence: msg.Fence, ReqID: msg.ReqID,
			Reason: reason, EpochFence: r.maxFence,
		})
		h.Log("resource.reject", map[string]any{
			"client": msg.Src, "fence": msg.Fence, "max_fence": r.maxFence,
			"req_id": msg.ReqID, "reason": reason,
		})
		h.Send(ack)
		return
	}

	c := Commit{At: t, Client: msg.Src, Fence: msg.Fence, ReqID: msg.ReqID, Value: msg.Value}
	r.committed = append(r.committed, c)
	r.maxFence = msg.Fence
	r.seen[msg.ReqID] = submitSeen{result: ResultCommitted, fence: msg.Fence}
	ack.Type = TypeSubmitAck
	ack.Result = ResultCommitted
	ack.Fence = msg.Fence
	ack.Value = msg.Value
	h.Log("resource.commit", map[string]any{
		"client": msg.Src, "fence": msg.Fence, "req_id": msg.ReqID, "value": msg.Value,
	})
	h.Send(ack)
}

// check returns "" when the write may be applied, otherwise the reject
// result code. Rule: a fence greater than every fence seen so far commits;
// anything smaller is a stale holder; zero means the caller never held the
// lock at all.
func (r *Resource) check(msg *Envelope) string {
	switch {
	case msg.Fence <= 0:
		return ResultFenceZero
	case msg.Fence < r.maxFence:
		return ResultStaleFence
	default:
		return ""
	}
}

func (r *Resource) OnTimer(Host, Time, string)       {}
func (r *Resource) OnAction(Host, Time, *ActionSpec) {}
func (r *Resource) OnPause(Host, Time)               {}
func (r *Resource) OnResume(Host, Time)              {}

// Snapshot returns the terminal resource state.
func (r *Resource) Snapshot() ResourceSnapshot {
	committed := make([]Commit, len(r.committed))
	copy(committed, r.committed)
	rejected := make([]RejectedSubmit, len(r.rejected))
	copy(rejected, r.rejected)
	snap := ResourceSnapshot{
		NodeID:      ResourceID,
		Committed:   committed,
		Rejected:    rejected,
		MaxFence:    r.maxFence,
		HolderFence: r.maxFence,
	}
	return snap
}
