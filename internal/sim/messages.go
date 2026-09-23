package sim

// Message Type values. Requests and replies are paired:
//
//	acquire.req -> acquire.ack (granted) or acquire.busy (held by someone else)
//	renew.req   -> renew.ack or renew.err (unknown/expired lease)
//	release.req -> release.ack or release.err
//	submit.req  -> submit.ack (committed, duplicate or rejected)
const (
	TypeAcquireReq  = "acquire.req"
	TypeAcquireAck  = "acquire.ack"
	TypeAcquireBusy = "acquire.busy"

	TypeRenewReq = "renew.req"
	TypeRenewAck = "renew.ack"
	TypeRenewErr = "renew.err"

	TypeReleaseReq = "release.req"
	TypeReleaseAck = "release.ack"
	TypeReleaseErr = "release.err"

	TypeSubmitReq = "submit.req"
	TypeSubmitAck = "submit.ack"
)

// ReqType identifies the operation of a request, for network fault rules
// and for receiver-side de-duplication. Replies carry the same ReqType as
// the request they answer.
const (
	ReqAcquire = "acquire"
	ReqRenew   = "renew"
	ReqRelease = "release"
	ReqSubmit  = "submit"
)

// Result values carried by acknowledgements.
const (
	ResultGranted   = "granted"
	ResultCommitted = "committed"
	ResultDuplicate = "duplicate" // same ReqID seen again; first outcome stands

	ResultBusy       = "busy"        // lock held by a live leaseholder
	ResultExpired    = "expired"     // renew/release against an expired lease
	ResultUnknown    = "unknown"     // renew/release of an id the service never saw
	ResultNotGranted = "not_granted" // client was told its lease is gone
	ResultStaleFence = "stale_fence" // resource: fence below the resource epoch
	ResultFenceZero  = "fence_zero"  // resource: write with no fence at all
)

// Envelope is the single wire format of the simulated network. Every packet
// is JSON-serializable so traces and scenario examples read as real requests.
type Envelope struct {
	ID       int64  `json:"id"`
	Src      string `json:"src"`
	Dst      string `json:"dst"`
	SendTime Time   `json:"send_time"`
	Type     string `json:"type"`
	ReqType  string `json:"req_type,omitempty"`
	ReqID    string `json:"req_id,omitempty"`

	// Lock lease fields.
	LeaseID  string `json:"lease_id,omitempty"`
	Fence    int64  `json:"fence,omitempty"`
	ExpireAt Time   `json:"expire_at,omitempty"`

	// Resource fields.
	Resource string `json:"resource,omitempty"`
	Value    string `json:"value,omitempty"`

	Result string `json:"result,omitempty"`
	Reason string `json:"reason,omitempty"`

	// DuplicateOf is nonzero on copies produced by the network fault model;
	// it carries the envelope ID of the original packet.
	DuplicateOf int64 `json:"duplicate_of,omitempty"`
}
