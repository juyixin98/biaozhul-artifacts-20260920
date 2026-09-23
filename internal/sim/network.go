package sim

// Match selects messages a network rule applies to. Empty fields are wildcards.
// Nth, when non-zero, restricts the rule to the Nth matching *send* (1-based),
// counted independently per rule — this lets a scenario target "the second
// renewal attempt" precisely instead of relying on a probability.
type Match struct {
	From   string `json:"from,omitempty"`
	To     string `json:"to,omitempty"`
	Method string `json:"method,omitempty"`
	Nth    int    `json:"nth,omitempty"`
}

// Rule is an explicit network fault. Actions:
//
//   - "drop":     the message (or its Nth occurrence) never arrives.
//   - "delay":    delivery is postponed by Delay ticks; a large delay models a
//     message that arrives late, after the lease has expired.
//   - "duplicate": an extra copy is delivered DuplicateDelay ticks after the
//     original (the original is delivered normally).
type Rule struct {
	Name     string `json:"name,omitempty"`
	Match    Match  `json:"match"`
	Action   string `json:"action"`
	Delay    int64  `json:"delay,omitempty"`
	DupDelay int64  `json:"duplicate_delay,omitempty"`
	seen     int
}

// NetConfig configures the probabilistic background behavior of the network.
// Explicit rules are always evaluated first; a message matched by a rule is
// not subject to the random defaults below.
type NetConfig struct {
	MinDelay      int64   `json:"min_delay"`      // baseline one-way delay
	MaxDelay      int64   `json:"max_delay"`      // extra uniform jitter range [0, MaxDelay]
	LossProb      float64 `json:"loss_prob"`      // probability a copy is dropped
	DuplicateProb float64 `json:"duplicate_prob"` // probability of an extra copy
	ReorderProb   float64 `json:"reorder_prob"`   // probability of extra shuffle delay
	Rules         []*Rule `json:"rules,omitempty"`
}

// Network decides, for every sent message, how many copies exist and when they
// arrive.
type Network struct {
	eng *Engine
	cfg NetConfig
}

// NewNetwork constructs the transport for an engine.
func NewNetwork(eng *Engine, cfg NetConfig) *Network {
	if cfg.MinDelay < 0 {
		cfg.MinDelay = 0
	}
	if cfg.MaxDelay < 0 {
		cfg.MaxDelay = 0
	}
	return &Network{eng: eng, cfg: cfg}
}

// AddRule installs an explicit rule at runtime (the runner does this for
// "add_rule" actions).
func (n *Network) AddRule(r *Rule) { n.cfg.Rules = append(n.cfg.Rules, r) }

func (n *Network) matches(m *Match, msg *Message) bool {
	if m.From != "" && m.From != msg.From {
		return false
	}
	if m.To != "" && m.To != msg.To {
		return false
	}
	if m.Method != "" && m.Method != msg.Method {
		return false
	}
	return true
}

// Route processes one send: explicit rules first, then stochastic defaults.
func (n *Network) Route(msg *Message) {
	for _, r := range n.cfg.Rules {
		if !n.matches(&r.Match, msg) {
			continue
		}
		r.seen++
		if r.Match.Nth != 0 && r.seen != r.Match.Nth {
			continue // waiting for the targeted occurrence
		}
		switch r.Action {
		case "drop":
			n.eng.Record("network", "net.drop", map[string]any{
				"rule": r.Name, "from": msg.From, "to": msg.To, "method": msg.Method,
			})
			return
		case "delay":
			at := n.eng.Now() + r.Delay
			n.eng.Record("network", "net.delay", map[string]any{
				"rule": r.Name, "method": msg.Method, "deliver_at": at,
			})
			n.eng.deliver(at, msg)
			return
		case "duplicate":
			n.eng.deliver(n.eng.Now()+n.baseDelay(), msg)
			dup := *msg
			dup.Attempt = msg.Attempt + 1
			n.eng.Record("network", "net.duplicate", map[string]any{
				"rule": r.Name, "method": msg.Method, "deliver_at": n.eng.Now() + r.DupDelay,
			})
			n.eng.deliver(n.eng.Now()+r.DupDelay, &dup)
			return
		default:
			panic("sim: unknown network rule action " + r.Action)
		}
	}

	// Random defaults.
	if n.eng.rng.Chance(n.cfg.LossProb) {
		n.eng.Record("network", "net.random_drop", map[string]any{"method": msg.Method})
		return
	}
	n.eng.deliver(n.eng.Now()+n.baseDelay(), msg)
	if n.eng.rng.Chance(n.cfg.DuplicateProb) {
		dup := *msg
		dup.Attempt = msg.Attempt + 1
		n.eng.deliver(n.eng.Now()+n.baseDelay()+n.baseDelay(), &dup)
	}
}

// baseDelay samples one one-way delay: MinDelay + jitter, optionally inflated
// when the reorder die succeeds (which lets later-sent messages overtake it).
func (n *Network) baseDelay() int64 {
	d := n.cfg.MinDelay
	if n.cfg.MaxDelay > 0 {
		d += int64(n.eng.rng.Intn(int(n.cfg.MaxDelay) + 1))
	}
	if n.eng.rng.Chance(n.cfg.ReorderProb) {
		d += n.cfg.MinDelay + n.cfg.MaxDelay + 1
	}
	return d
}
