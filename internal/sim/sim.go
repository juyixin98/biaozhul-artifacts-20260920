package sim

import (
	"container/heap"
	"sort"

	"causal-broadcast/internal/netlink"
	"causal-broadcast/internal/node"
)

// Sim is a configured simulator instance.
type Sim struct {
	req    Request
	n      int
	nodes  []*node.Node
	net    *netlink.Network
	events eventHeap

	counter int64

	now float64

	// per-node counters
	arrivals   []int // packet copies that showed up (incl. duplicates)
	duplicates []int // arrivals recognized as already delivered/buffered
	backpress  []int // arrivals rejected because the buffer was full
	buffered   []int // arrivals accepted into the buffer
	released   []int // buffered messages released by a later arrival

	// senderSeq[node] is the next broadcast sequence number.
	senderSeq []int

	registry map[string]node.Message

	deliveries []Delivery
	trace      []TraceEvent

	// pending arrivals not yet processed at the cutoff.
	pendingArrivals int
	pendingIDs      map[string]bool // key: dst index + "/" + msgID

	stats Stats
}

// Result is the complete simulator output.
type Result struct {
	Now             float64 `json:"now"`
	Complete        bool    `json:"complete"`
	Names           []string
	Deliveries      []Delivery   `json:"deliveries"`
	Trace           []TraceEvent `json:"trace"`
	Nodes           []NodeFinal  `json:"nodes"`
	PendingArrivals int          `json:"pendingArrivals"`
	Stats           Stats        `json:"stats"`

	Diagnostics Diagnostics `json:"diagnostics"`
}

// Stats holds run-wide counters.
type Stats struct {
	Broadcasts       int `json:"broadcasts"`
	Arrivals         int `json:"arrivals"`
	DeliveredNew     int `json:"deliveredNew"`
	Duplicates       int `json:"duplicates"`
	BufferedTotal    int `json:"bufferedTotal"`
	ReleasedFromBuf  int `json:"releasedFromBuffer"`
	Backpressure     int `json:"backpressure"`
	ForcedDropped    int `json:"forcedDropped"`
	NetworkDropped   int `json:"networkDropped"`
	InFlightAtCutoff int `json:"inFlightAtCutoff"`
}

// New constructs a simulator from a request.
func New(req Request) *Sim {
	n := len(req.Names)
	s := &Sim{
		req:        req,
		n:          n,
		arrivals:   make([]int, n),
		duplicates: make([]int, n),
		backpress:  make([]int, n),
		buffered:   make([]int, n),
		released:   make([]int, n),
		senderSeq:  make([]int, n),
		registry:   make(map[string]node.Message),
		pendingIDs: make(map[string]bool),
	}

	caps := req.NodeBuffer
	s.nodes = make([]*node.Node, n)
	for i, name := range req.Names {
		cap := req.BufferCap
		if caps != nil {
			if c, ok := caps[name]; ok {
				cap = c
			}
		}
		s.nodes[i] = node.New(i, n, cap, req.Names)
	}

	s.net = netlink.New(n, req.DefaultLink, req.Links, req.Seed)
	for id, dsts := range req.DropIDs {
		if len(dsts) == 0 {
			s.net.ForceDrop(id, -1)
		} else {
			for _, d := range dsts {
				s.net.ForceDrop(id, d)
			}
		}
	}
	for id, perDst := range req.HoldIDs {
		for dst, delays := range perDst {
			for _, d := range delays {
				s.net.ForceHold(id, dst, d)
			}
		}
	}
	for id, perDst := range req.DelayedIDs {
		for dst, delays := range perDst {
			s.net.ForceDelay(id, dst, delays...)
		}
	}

	// Schedule broadcasts; stable order by (time, sender) gives deterministic
	// insertion sequences even when two broadcasts share a timestamp.
	bc := make([]BroadcastSpec, len(req.Broadcasts))
	copy(bc, req.Broadcasts)
	sort.SliceStable(bc, func(i, j int) bool {
		if bc[i].Time != bc[j].Time {
			return bc[i].Time < bc[j].Time
		}
		return bc[i].Sender < bc[j].Sender
	})
	for _, b := range bc {
		s.push(&event{
			time:   b.Time,
			kind:   evBroadcast,
			sender: b.Sender,
			dst:    -1,
			body:   b.Body,
		})
	}
	return s
}

// Run processes events until the heap drains or MaxTime is reached.
func (s *Sim) Run() *Result {
	complete := true
	for s.events.Len() > 0 {
		e := heap.Pop(&s.events).(*event)
		if s.req.MaxTime > 0 && e.time > s.req.MaxTime {
			// This and every remaining event lie beyond the cutoff. Record
			// unprocessed arrivals so they can be reported as in-flight.
			complete = false
			s.markPending(e)
			for _, pe := range s.events {
				s.markPending(pe)
			}
			s.pendingArrivals = len(s.pendingIDs)
			break
		}
		s.now = e.time
		switch e.kind {
		case evBroadcast:
			s.processBroadcast(e)
		case evArrival:
			s.processArrival(e)
		}
	}
	s.stats.InFlightAtCutoff = s.pendingArrivals
	s.stats.Broadcasts = len(s.registry)
	for i := range s.arrivals {
		s.stats.Arrivals += s.arrivals[i]
		s.stats.Duplicates += s.duplicates[i]
		s.stats.Backpressure += s.backpress[i]
		s.stats.BufferedTotal += s.buffered[i]
		s.stats.ReleasedFromBuf += s.released[i]
	}
	// DeliveredNew counts distinct (node,message) deliveries.
	totalDelivered := 0
	for _, nd := range s.nodes {
		totalDelivered += nd.DeliveredCount()
	}
	// Subtract local deliveries? Local deliveries are real deliveries, so
	// keep them counted; the figure is "application deliveries".
	s.stats.DeliveredNew = totalDelivered

	res := &Result{
		Now:             s.now,
		Complete:        complete,
		Names:           s.req.Names,
		Deliveries:      s.deliveries,
		Trace:           s.trace,
		PendingArrivals: s.pendingArrivals,
		Stats:           s.stats,
	}
	s.buildNodeFinals(res)
	s.buildDiagnostics(res)
	return res
}

func pendingKey(dst int, msgID string) string {
	return itoa(dst) + "/" + msgID
}

func (s *Sim) markPending(e *event) {
	if e.kind == evArrival {
		s.pendingIDs[pendingKey(e.dst, e.msg.ID)] = true
	}
}

func (s *Sim) processBroadcast(e *event) {
	sender := e.sender
	s.senderSeq[sender]++
	seq := s.senderSeq[sender]

	// The broadcaster's clock at broadcast time is its current clock plus
	// its own increment: the message depends on everything delivered so far.
	clock := s.nodes[sender].Clock()
	clock[sender]++

	id := s.req.Names[sender] + itoa(seq)
	msg := node.Message{
		ID:     id,
		Sender: sender,
		Seq:    seq,
		Clock:  clock,
		Body:   e.body,
	}
	s.registry[id] = msg

	s.trace = append(s.trace, TraceEvent{
		Time:  s.now,
		Type:  "broadcast",
		Node:  s.req.Names[sender],
		MsgID: id,
		Clock: s.clockMap(clock),
	})

	// Local delivery first, then fan out over the simulated links.
	res := s.nodes[sender].Receive(msg)
	s.recordDeliveries(sender, res, true)

	for dst := 0; dst < s.n; dst++ {
		if dst == sender {
			continue
		}
		plan := s.net.Send(id, sender, dst, s.now)
		if plan.DropReason != "" {
			if plan.DropReason == "forced" {
				s.stats.ForcedDropped++
			} else {
				s.stats.NetworkDropped++
			}
			s.trace = append(s.trace, TraceEvent{
				Time:   s.now,
				Type:   "drop",
				Node:   s.req.Names[sender],
				Dst:    s.req.Names[dst],
				MsgID:  id,
				Reason: plan.DropReason,
			})
			continue
		}
		for ci, at := range plan.Arrivals {
			s.push(&event{
				time:      at,
				kind:      evArrival,
				sender:    sender,
				dst:       dst,
				msg:       msg,
				scheduled: at,
				copyIndex: ci,
			})
		}
	}
}

func (s *Sim) processArrival(e *event) {
	dst := e.dst
	n := s.nodes[dst]
	s.arrivals[dst]++

	res := n.Receive(e.msg)
	switch res.Newly {
	case node.OutcomeDuplicate:
		s.duplicates[dst]++
		s.trace = append(s.trace, TraceEvent{
			Time:        s.now,
			Type:        "duplicate",
			Node:        s.req.Names[dst],
			MsgID:       e.msg.ID,
			ArrivedAt:   s.now,
			ScheduledAt: e.scheduled,
		})
	case node.OutcomeBackpressure:
		s.backpress[dst]++
		s.trace = append(s.trace, TraceEvent{
			Time:       s.now,
			Type:       "backpressure",
			Node:       s.req.Names[dst],
			MsgID:      e.msg.ID,
			Reason:     "buffer-full",
			BufferSize: n.BufferedCount(),
			BufferCap:  n.BufferCap(),
		})
	case node.OutcomeBuffered:
		s.buffered[dst]++
		s.trace = append(s.trace, TraceEvent{
			Time:       s.now,
			Type:       "buffered",
			Node:       s.req.Names[dst],
			MsgID:      e.msg.ID,
			BufferSize: n.BufferedCount(),
			BufferCap:  n.BufferCap(),
		})
	case node.OutcomeDelivered:
		s.recordDeliveries(dst, res, false)
	}
}

// recordDeliveries turns a ReceiveResult that delivered one or more messages
// into delivery records and trace entries.
func (s *Sim) recordDeliveries(at int, res node.ReceiveResult, local bool) {
	kind := "deliver"
	if local {
		kind = "deliver-local"
	}
	for idx, d := range res.Delivered {
		m := d.Msg
		reason := ""
		if !local || idx > 0 {
			// Everything after the triggering arrival was released from the
			// buffer (including the local-delivery cascade after a broadcast).
			if idx > 0 {
				reason = "cascade"
				s.released[at]++
			}
		}
		s.deliveries = append(s.deliveries, Delivery{
			Time:  s.now,
			Node:  s.req.Names[at],
			MsgID: m.ID,
			Clock: s.clockMap(d.ClockAfter),
		})
		s.trace = append(s.trace, TraceEvent{
			Time:   s.now,
			Type:   kind,
			Node:   s.req.Names[at],
			MsgID:  m.ID,
			Reason: reason,
			Clock:  s.clockMap(d.ClockAfter),
		})
	}
}

func (s *Sim) clockMap(c node.VC) map[string]int {
	out := make(map[string]int, s.n)
	for i := range c {
		if c[i] != 0 {
			out[s.req.Names[i]] = c[i]
		}
	}
	return out
}

// itoa avoids importing strconv for a tiny hot path.
func itoa(x int) string {
	if x == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for x > 0 {
		i--
		buf[i] = byte('0' + x%10)
		x /= 10
	}
	return string(buf[i:])
}
