// Package jsonio defines the JSON run interface: request/response shapes,
// permissive parsing of convenience fields (holdIds may be a number or a
// list), validation, and conversion into the simulator-native request.
package jsonio

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"causal-broadcast/internal/netlink"
	"causal-broadcast/internal/sim"
)

// Request is the JSON run request.
type Request struct {
	Nodes      []string       `json:"nodes"`
	BufferCap  int            `json:"bufferCap"`
	NodeBuffer map[string]int `json:"nodeBuffer"`

	Network NetworkConfig `json:"network"`

	// MaxTime stops the run at this simulated time (>0 only); events still in
	// flight are reported as blocked/in-flight rather than permanently lost.
	MaxTime float64 `json:"maxTime"`

	Broadcasts []BroadcastJSON `json:"broadcasts"`
}

// NetworkConfig configures the link fabric.
type NetworkConfig struct {
	Seed       *int64      `json:"seed"`
	Default    *LinkJSON   `json:"default"`
	Links      []LinkEntry `json:"links"`
	DropIDs    []DropEntry `json:"dropIds"`
	HoldIDs    []HoldEntry `json:"holdIds"`
	DelayedIDs []HoldEntry `json:"delayedIds"`
}

// LinkJSON is one link's probabilities and timings.
type LinkJSON struct {
	Loss       *float64 `json:"loss"`
	Duplicate  *float64 `json:"duplicate"`
	BaseDelay  *float64 `json:"baseDelay"`
	Jitter     *float64 `json:"jitter"`
	ReorderJit *float64 `json:"reorderJitter"`
}

// LinkEntry is a per-pair link override.
type LinkEntry struct {
	From string   `json:"from"`
	To   string   `json:"to"`
	Link LinkJSON `json:"link"`
}

// DropEntry forces drops of MsgID. Dst omitted/empty means every node.
type DropEntry struct {
	MsgID string   `json:"msgId"`
	Dst   []string `json:"dst"`
}

// HoldEntry forces extra delayed copies of MsgID.
//
// Delay may be a number (one extra copy arriving at that absolute delay)
// or a list (several extra copies). Dst omitted means every node.
type HoldEntry struct {
	MsgID string    `json:"msgId"`
	Dst   []string  `json:"dst"`
	Delay FlexFloat `json:"delay"`
}

// BroadcastJSON schedules one broadcast.
type BroadcastJSON struct {
	Time float64 `json:"time"`
	From string  `json:"from"`
	Body string  `json:"body"`
}

// FlexFloat accepts either a single number or a list of numbers in JSON.
type FlexFloat []float64

func (f *FlexFloat) UnmarshalJSON(b []byte) error {
	var single float64
	if err := json.Unmarshal(b, &single); err == nil {
		*f = []float64{single}
		return nil
	}
	var many []float64
	if err := json.Unmarshal(b, &many); err != nil {
		return fmt.Errorf("delay must be a number or an array of numbers")
	}
	*f = many
	return nil
}

// DefaultBufferCap is used when bufferCap is omitted.
const DefaultBufferCap = 1024

// Validate checks the request and fills defaults.
func (r *Request) Validate() error {
	if len(r.Nodes) < 1 {
		return fmt.Errorf("nodes: at least one node is required")
	}
	seen := map[string]bool{}
	for _, n := range r.Nodes {
		if n == "" {
			return fmt.Errorf("nodes: node names must not be empty")
		}
		if seen[n] {
			return fmt.Errorf("nodes: duplicate node name %q", n)
		}
		seen[n] = true
	}
	if r.BufferCap == 0 && len(r.NodeBuffer) == 0 {
		r.BufferCap = DefaultBufferCap
	}
	for n := range r.NodeBuffer {
		if !seen[n] {
			return fmt.Errorf("nodeBuffer: unknown node %q", n)
		}
	}
	if r.Network.Default != nil {
		if err := r.Network.Default.validate("network.default"); err != nil {
			return err
		}
	}
	for i, le := range r.Network.Links {
		if !seen[le.From] || !seen[le.To] {
			return fmt.Errorf("network.links[%d]: unknown endpoint (%s -> %s)", i, le.From, le.To)
		}
		if le.From == le.To {
			return fmt.Errorf("network.links[%d]: self link %s is not allowed", i, le.From)
		}
		if err := le.Link.validate(fmt.Sprintf("network.links[%d].link", i)); err != nil {
			return err
		}
	}
	for i, d := range r.Network.DropIDs {
		if d.MsgID == "" {
			return fmt.Errorf("network.dropIds[%d]: msgId is required", i)
		}
		for _, dst := range d.Dst {
			if !seen[dst] {
				return fmt.Errorf("network.dropIds[%d]: unknown dst %q", i, dst)
			}
		}
	}
	for i, h := range r.Network.HoldIDs {
		if err := validateHold(h, fmt.Sprintf("network.holdIds[%d]", i), seen); err != nil {
			return err
		}
	}
	for i, h := range r.Network.DelayedIDs {
		if err := validateHold(h, fmt.Sprintf("network.delayedIds[%d]", i), seen); err != nil {
			return err
		}
	}
	for i, b := range r.Broadcasts {
		if !seen[b.From] {
			return fmt.Errorf("broadcasts[%d]: unknown from %q", i, b.From)
		}
		if b.Time < 0 {
			return fmt.Errorf("broadcasts[%d]: time must be >= 0", i)
		}
	}
	if r.MaxTime < 0 {
		return fmt.Errorf("maxTime must be >= 0")
	}
	return nil
}

func validateHold(h HoldEntry, path string, seen map[string]bool) error {
	if h.MsgID == "" {
		return fmt.Errorf("%s: msgId is required", path)
	}
	if len(h.Delay) == 0 {
		return fmt.Errorf("%s: delay is required", path)
	}
	for _, d := range h.Delay {
		if d < 0 {
			return fmt.Errorf("%s: delay must be >= 0", path)
		}
	}
	for _, dst := range h.Dst {
		if !seen[dst] {
			return fmt.Errorf("%s: unknown dst %q", path, dst)
		}
	}
	return nil
}

func (l *LinkJSON) validate(path string) error {
	check := func(v *float64, name string) error {
		if v != nil && (*v < 0 || *v > 1) {
			return fmt.Errorf("%s.%s: probability must be in [0,1]", path, name)
		}
		return nil
	}
	if err := check(l.Loss, "loss"); err != nil {
		return err
	}
	if err := check(l.Duplicate, "duplicate"); err != nil {
		return err
	}
	if l.BaseDelay != nil && *l.BaseDelay < 0 {
		return fmt.Errorf("%s.baseDelay: must be >= 0", path)
	}
	if l.Jitter != nil && *l.Jitter < 0 {
		return fmt.Errorf("%s.jitter: must be >= 0", path)
	}
	if l.ReorderJit != nil && *l.ReorderJit < 0 {
		return fmt.Errorf("%s.reorderJitter: must be >= 0", path)
	}
	return nil
}

// ToSim converts the JSON request to the native simulator request.
func (r *Request) ToSim() (sim.Request, error) {
	if err := r.Validate(); err != nil {
		return sim.Request{}, err
	}
	index := map[string]int{}
	for i, n := range r.Nodes {
		index[n] = i
	}

	def := netlink.Link{BaseDelay: 1.0}
	mergeLink(&def, r.Network.Default)

	overrides := map[[2]int]netlink.Link{}
	for _, le := range r.Network.Links {
		l := def
		mergeLink(&l, &le.Link)
		overrides[[2]int{index[le.From], index[le.To]}] = l
	}

	drop := map[string][]int{}
	for _, d := range r.Network.DropIDs {
		var dsts []int
		for _, name := range d.Dst {
			dsts = append(dsts, index[name])
		}
		drop[d.MsgID] = append(drop[d.MsgID], dsts...)
	}

	holds := map[string]map[int][]float64{}
	delayed := map[string]map[int][]float64{}
	addHold := func(target map[string]map[int][]float64, id string, dst int, delays []float64) {
		m, ok := target[id]
		if !ok {
			m = map[int][]float64{}
			target[id] = m
		}
		m[dst] = append(m[dst], delays...)
	}
	for _, h := range r.Network.HoldIDs {
		if len(h.Dst) == 0 {
			// The network layer interprets -1 as all nodes.
			addHold(holds, h.MsgID, -1, []float64(h.Delay))
			continue
		}
		for _, name := range h.Dst {
			addHold(holds, h.MsgID, index[name], []float64(h.Delay))
		}
	}
	for _, h := range r.Network.DelayedIDs {
		if len(h.Dst) == 0 {
			return sim.Request{}, fmt.Errorf("network.delayedIds: %s requires explicit dst", h.MsgID)
		}
		for _, name := range h.Dst {
			addHold(delayed, h.MsgID, index[name], []float64(h.Delay))
		}
	}

	var seed int64 = 1
	if r.Network.Seed != nil {
		seed = *r.Network.Seed
	}

	bc := make([]sim.BroadcastSpec, 0, len(r.Broadcasts))
	for _, b := range r.Broadcasts {
		bc = append(bc, sim.BroadcastSpec{
			Time: b.Time, Sender: index[b.From], Body: b.Body,
		})
	}

	return sim.Request{
		Names:       append([]string(nil), r.Nodes...),
		BufferCap:   r.BufferCap,
		NodeBuffer:  r.NodeBuffer,
		DefaultLink: def,
		Links:       overrides,
		Seed:        seed,
		Broadcasts:  bc,
		MaxTime:     r.MaxTime,
		DropIDs:     drop,
		HoldIDs:     holds,
		DelayedIDs:  delayed,
	}, nil
}

func mergeLink(dst *netlink.Link, src *LinkJSON) {
	if src == nil {
		return
	}
	if src.Loss != nil {
		dst.Loss = *src.Loss
	}
	if src.Duplicate != nil {
		dst.Duplicate = *src.Duplicate
	}
	if src.BaseDelay != nil {
		dst.BaseDelay = *src.BaseDelay
	}
	if src.Jitter != nil {
		dst.Jitter = *src.Jitter
	}
	if src.ReorderJit != nil {
		dst.ReorderJit = *src.ReorderJit
	}
}

// Run parses, validates and executes one JSON request, returning the result
// ready to be marshalled.
func Run(raw []byte) (*sim.Result, error) {
	var req Request
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		return nil, fmt.Errorf("invalid request JSON: %w", err)
	}
	sreq, err := req.ToSim()
	if err != nil {
		return nil, err
	}
	return sim.New(sreq).Run(), nil
}

// ParseMsgID splits an id like "A3" into ("A", 3) using the known node
// names. It accepts multi-character node names.
func ParseMsgID(id string, names []string) (string, int, error) {
	sorted := append([]string(nil), names...)
	sort.Slice(sorted, func(i, j int) bool { return len(sorted[i]) > len(sorted[j]) })
	for _, n := range sorted {
		if strings.HasPrefix(id, n) {
			rest := id[len(n):]
			seq, err := strconv.Atoi(rest)
			if err != nil || seq <= 0 {
				return "", 0, fmt.Errorf("invalid message id %q", id)
			}
			return n, seq, nil
		}
	}
	return "", 0, fmt.Errorf("invalid message id %q: unknown sender", id)
}
