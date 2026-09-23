package sim

// Fault types usable in scenario JSON (rules) and in Network config.
const (
	FaultDrop      = "drop"      // the packet is never delivered
	FaultDuplicate = "duplicate" // original plus one delayed copy
	FaultDelay     = "delay"     // held back for an extra delay
	FaultReorder   = "reorder"   // the same as delay, by convention
)

// Rule is a scripted, time-bounded network fault. Rules are checked in order
// and the first match wins, so exact rules can shadow probabilistic behavior.
type Rule struct {
	Type    string  `json:"type"`
	Src     string  `json:"src,omitempty"`
	Dst     string  `json:"dst,omitempty"`
	ReqType string  `json:"req_type,omitempty"`
	MsgType string  `json:"msg_type,omitempty"`
	From    Time    `json:"from,omitempty"`
	Until   Time    `json:"until,omitempty"`
	Delay   Time    `json:"delay_ms,omitempty"`
	Prob    float64 `json:"prob,omitempty"` // optional: only apply with probability

	applied int
}

// Match reports whether the rule selects msg at time t.
func (r *Rule) Match(t Time, msg *Envelope) bool {
	if r.Src != "" && r.Src != "*" && r.Src != msg.Src {
		return false
	}
	if r.Dst != "" && r.Dst != "*" && r.Dst != msg.Dst {
		return false
	}
	if r.ReqType != "" && r.ReqType != msg.ReqType {
		return false
	}
	if r.MsgType != "" && r.MsgType != msg.Type {
		return false
	}
	if r.From != 0 && t < r.From {
		return false
	}
	if r.Until != 0 && t > r.Until {
		return false
	}
	return true
}

// Stats counts what the network did. Counters are part of the run report.
type Stats struct {
	Sent       int64 `json:"sent"`
	Delivered  int64 `json:"delivered"` // scheduled deliveries, including copies
	Dropped    int64 `json:"dropped"`
	Duplicated int64 `json:"duplicated"`
	Delayed    int64 `json:"delayed"`
	PausedDrop int64 `json:"paused_sender_dropped"`
}

// Network is the transmission model. One link for all node pairs; per-pair
// behavior is expressed through rules and node filters.
type Network struct {
	// Baseline link behavior.
	MinLatency Time `json:"min_latency_ms"`
	MaxLatency Time `json:"max_latency_ms"`

	// Probabilistic faults applied when no scripted rule matches.
	DropProb      float64 `json:"drop_prob"`
	DuplicateProb float64 `json:"duplicate_prob"`
	DuplicateGap  Time    `json:"duplicate_gap_ms"`

	Rules []*Rule `json:"rules"`

	Stats Stats `json:"stats"`
}

type offer struct {
	msg       *Envelope
	deliverAt Time
}

// Transmit applies the fault model to one outbound message and returns the
// delivery offers for the engine to schedule.
func (n *Network) Transmit(h Host, msg *Envelope, senderPaused bool) []offer {
	rng := h.Rng()
	n.Stats.Sent++

	if senderPaused {
		n.Stats.Dropped++
		n.Stats.PausedDrop++
		h.Log("net.drop", map[string]any{
			"msg": msg.ID, "type": msg.Type, "src": msg.Src, "dst": msg.Dst,
			"reason": "sender_paused",
		})
		return nil
	}

	base := n.baseLatency(rng)
	now := h.Now()
	drop := rng.Bernoulli(n.DropProb)
	dup := rng.Bernoulli(n.DuplicateProb)
	var rule *Rule
	for _, r := range n.Rules {
		if r.Match(now, msg) && (r.Prob == 0 || rng.Bernoulli(r.Prob)) {
			rule = r
			break
		}
	}
	if rule != nil {
		r := rule
		r.applied++
		switch r.Type {
		case FaultDrop:
			drop = true
		case FaultDuplicate:
			dup = true
			// The original is also held back when the rule specifies a gap,
			// so a copy cannot beat the intended late arrival.
			if r.Delay > 0 {
				base += r.Delay
			}
		case FaultDelay, FaultReorder:
			base += r.Delay
		}
	}

	if drop {
		n.Stats.Dropped++
		h.Log("net.drop", map[string]any{
			"msg": msg.ID, "type": msg.Type, "src": msg.Src, "dst": msg.Dst,
			"rule": matchedRuleName(rule),
		})
		return nil
	}

	out := []offer{{msg: msg, deliverAt: now + base}}
	n.Stats.Delivered++

	if dup {
		// The duplicate copy follows the (possibly delayed) original by a
		// small copy gap; rule.Delay shifts the original, not the gap.
		gap := n.DuplicateGap
		if rule != nil && rule.Type == FaultDuplicate {
			gap = 50
		}
		if gap <= 0 {
			gap = 50
		}
		copyMsg := *msg
		copyMsg.DuplicateOf = msg.ID
		out = append(out, offer{msg: &copyMsg, deliverAt: now + base + gap})
		n.Stats.Duplicated++
		n.Stats.Delivered++
		h.Log("net.duplicate", map[string]any{
			"msg": msg.ID, "copy_gap": gap,
			"original_at": now + base, "copy_at": now + base + gap,
			"rule": matchedRuleName(rule),
		})
	}
	if rule != nil && (rule.Type == FaultDelay || rule.Type == FaultReorder) {
		n.Stats.Delayed++
		h.Log("net.delay", map[string]any{
			"msg": msg.ID, "extra_ms": rule.Delay,
			"deliver_at": now + base, "rule": matchedRuleName(rule),
		})
	}
	return out
}

func matchedRuleName(r *Rule) string {
	if r == nil {
		return "probabilistic"
	}
	return r.Type
}

func (n *Network) baseLatency(rng *Rng) Time {
	min, max := n.MinLatency, n.MaxLatency
	if max <= min {
		if max < 0 {
			return 0
		}
		return max
	}
	return min + Time(rng.Intn(int(max-min+1)))
}
