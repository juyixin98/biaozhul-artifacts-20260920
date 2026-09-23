package sim

import (
	"sort"

	"causal-broadcast/internal/node"
)

// MissingReport diagnoses one message that will never be delivered at a
// node in a fully drained run.
type MissingReport struct {
	Node       string         `json:"node"`
	MsgID      string         `json:"msgId"`
	Sender     string         `json:"sender"`
	State      string         `json:"state"` // "buffered" | "never-arrived"
	Clock      map[string]int `json:"clock"`
	Missing    []MissingDTO   `json:"missing,omitempty"`
	RootCauses []string       `json:"rootCauses,omitempty"`
	RootChain  []string       `json:"rootChain,omitempty"`
}

// BlockedReport describes one message still held when the run ended at a
// time cutoff (it may yet arrive, so it is "blocked", not "permanently
// missing").
type BlockedReport struct {
	Node    string         `json:"node"`
	MsgID   string         `json:"msgId"`
	Sender  string         `json:"sender"`
	State   string         `json:"state"` // "buffered" | "in-flight" | "never-arrived"
	Clock   map[string]int `json:"clock"`
	Missing []MissingDTO   `json:"missing,omitempty"`
}

// CausalViolation reports a delivery that broke vector-clock causal order.
type CausalViolation struct {
	Node   string `json:"node"`
	MsgID  string `json:"msgId"`
	Detail string `json:"detail"`
}

// Diagnostics aggregates all end-of-run findings.
type Diagnostics struct {
	PermanentMissing []MissingReport   `json:"permanentMissing"`
	Blocked          []BlockedReport   `json:"blocked"`
	CausalOrderOK    bool              `json:"causalOrderOk"`
	Violations       []CausalViolation `json:"violations"`
}

func (s *Sim) buildNodeFinals(res *Result) {
	res.Nodes = make([]NodeFinal, 0, s.n)
	for i, nd := range s.nodes {
		nf := NodeFinal{
			Name:            s.req.Names[i],
			Clock:           s.clockMap(nd.Clock()),
			DeliveredCount:  nd.DeliveredCount(),
			BufferHighWater: nd.HighWater(),
		}
		for _, b := range nd.Snapshot() {
			dto := BufferedDTO{
				MsgID:  b.Msg.ID,
				Sender: s.req.Names[b.Msg.Sender],
				Clock:  s.clockMap(b.Msg.Clock),
			}
			missing, _ := nd.MissingDepsFor(b.Msg.ID)
			for _, m := range missing {
				dto.Missing = append(dto.Missing, MissingDTO{
					From: s.req.Names[m.From],
					Have: m.Have,
					Need: m.Need,
				})
			}
			nf.Buffered = append(nf.Buffered, dto)
		}
		res.Nodes = append(res.Nodes, nf)
	}
}

func (s *Sim) buildDiagnostics(res *Result) {
	d := Diagnostics{CausalOrderOK: true}

	// Deterministic message ordering for reports.
	allMsgs := make([]node.Message, 0, len(s.registry))
	for _, m := range s.registry {
		allMsgs = append(allMsgs, m)
	}
	sort.Slice(allMsgs, func(i, j int) bool {
		a, b := allMsgs[i], allMsgs[j]
		if a.Sender != b.Sender {
			return a.Sender < b.Sender
		}
		return a.Seq < b.Seq
	})

	for i := range s.nodes {
		nd := s.nodes[i]
		for _, m := range allMsgs {
			if m.Sender == i || nd.Delivered(m.ID) {
				continue
			}
			if nd.InBuffer(m.ID) {
				missing, _ := nd.MissingDepsFor(m.ID)
				dto := []MissingDTO{}
				for _, mm := range missing {
					dto = append(dto, MissingDTO{
						From: s.req.Names[mm.From], Have: mm.Have, Need: mm.Need,
					})
				}
				chain, roots := s.rootChain(i, m)
				report := MissingReport{
					Node:       s.req.Names[i],
					MsgID:      m.ID,
					Sender:     s.req.Names[m.Sender],
					State:      "buffered",
					Clock:      s.clockMap(m.Clock),
					Missing:    dto,
					RootChain:  chain,
					RootCauses: roots,
				}
				if res.Complete {
					d.PermanentMissing = append(d.PermanentMissing, report)
				} else {
					d.Blocked = append(d.Blocked, BlockedReport{
						Node: report.Node, MsgID: report.MsgID, Sender: report.Sender,
						State: "buffered", Clock: report.Clock, Missing: dto,
					})
				}
				continue
			}
			// Not delivered, not buffered.
			state := "never-arrived"
			if !res.Complete && s.pendingIDs[pendingKey(i, m.ID)] {
				state = "in-flight"
			}
			missing := node.MissingDeps(nd.Clock(), m.Clock, m.Sender)
			dto := []MissingDTO{}
			for _, mm := range missing {
				dto = append(dto, MissingDTO{
					From: s.req.Names[mm.From], Have: mm.Have, Need: mm.Need,
				})
			}
			if res.Complete && state == "never-arrived" {
				chain, roots := s.rootChainForAbsent(i, m)
				d.PermanentMissing = append(d.PermanentMissing, MissingReport{
					Node: s.req.Names[i], MsgID: m.ID,
					Sender: s.req.Names[m.Sender], State: state,
					Clock: s.clockMap(m.Clock), Missing: dto,
					RootChain: chain, RootCauses: roots,
				})
			} else if !res.Complete {
				d.Blocked = append(d.Blocked, BlockedReport{
					Node: s.req.Names[i], MsgID: m.ID,
					Sender: s.req.Names[m.Sender], State: state,
					Clock: s.clockMap(m.Clock), Missing: dto,
				})
			}
		}
	}

	d.Violations = s.checkCausalOrder()
	d.CausalOrderOK = len(d.Violations) == 0

	sortReports(d.PermanentMissing)
	sortBlocked(d.Blocked)
	res.Diagnostics = d
}

// rootChain walks buffered dependencies backwards from a buffered message
// until it reaches messages that are absent at this node ("A1@C"). It
// returns the human-readable chain and the set of terminal root causes.
//
// Example: C3 -> A2 -> A1
func (s *Sim) rootChain(at int, m node.Message) (chain []string, roots []string) {
	return s.walk(at, m, map[string]bool{}, map[string]bool{})
}

func (s *Sim) walk(at int, m node.Message, visitedMsg, visitedRoot map[string]bool) ([]string, []string) {
	var chain []string
	var roots []string
	add := func(x string) {
		if !visitedMsg[x] {
			visitedMsg[x] = true
			chain = append(chain, x)
		}
	}
	addRoot := func(x string) {
		if !visitedRoot[x] {
			visitedRoot[x] = true
			roots = append(roots, x)
		}
	}

	// Find the most urgent missing requirement of m at node `at`, then move
	// to that predecessor.
	cur := m
	add(cur.ID)
	for {
		nd := s.nodes[at]
		var miss []node.Missing
		if nd.InBuffer(cur.ID) {
			miss, _ = nd.MissingDepsFor(cur.ID)
		} else {
			miss = node.MissingDeps(nd.Clock(), cur.Clock, cur.Sender)
		}
		if len(miss) == 0 {
			break
		}
		sort.Slice(miss, func(i, j int) bool {
			if miss[i].From != miss[j].From {
				return miss[i].From < miss[j].From
			}
			return miss[i].Need < miss[j].Need
		})
		req := miss[0]
		predID := s.req.Names[req.From] + itoa(req.Need)
		pred, known := s.registry[predID]

		// The predecessor is the root if it never arrived at this node.
		if !nd.InBuffer(predID) && !nd.Delivered(predID) {
			addRoot(predID + "@" + s.req.Names[at])
			add(predID)
			break
		}
		if !known {
			// Sequence beyond what the sender ever broadcast: gap in the
			// sender's own stream.
			addRoot(predID + "@" + s.req.Names[at])
			add(predID)
			break
		}
		if visitedMsg[predID] {
			break
		}
		add(predID)
		cur = pred
	}
	return chain, roots
}

// rootChainForAbsent handles a message that never arrived at all: its root
// cause is itself, unless its sender's earlier gap explains it.
func (s *Sim) rootChainForAbsent(at int, m node.Message) (chain []string, roots []string) {
	nd := s.nodes[at]
	// Own-sender gap: A3 lost while A2 also unknown -> the chain stops at
	// the earliest missing own-sequence.
	if m.Seq > nd.Clock()[m.Sender]+1 {
		earliestSeq := nd.Clock()[m.Sender] + 1
		predID := s.req.Names[m.Sender] + itoa(earliestSeq)
		return []string{m.ID, predID}, []string{predID + "@" + s.req.Names[at]}
	}
	// Cross-node predecessor gaps.
	miss := node.MissingDeps(nd.Clock(), m.Clock, m.Sender)
	sort.Slice(miss, func(i, j int) bool {
		if miss[i].From != miss[j].From {
			return miss[i].From < miss[j].From
		}
		return miss[i].Need < miss[j].Need
	})
	roots = append(roots, m.ID+"@"+s.req.Names[at])
	chain = []string{m.ID}
	for _, mm := range miss {
		predID := s.req.Names[mm.From] + itoa(mm.Need)
		roots = append(roots, predID+"@"+s.req.Names[at])
		chain = append(chain, predID)
	}
	return chain, dedup(roots)
}

func dedup(in []string) []string {
	seen := map[string]bool{}
	out := in[:0]
	for _, x := range in {
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	return out
}

// nameIndex finds a node's index by name, or -1.
func (s *Sim) nameIndex(name string) int {
	for i, n := range s.req.Names {
		if n == name {
			return i
		}
	}
	return -1
}

// checkCausalOrder replays the recorded delivery log per node and verifies
// every delivery actually satisfied the vector-clock rule. (The simulator
// should never produce a violation; this independently re-checks it.)
func (s *Sim) checkCausalOrder() []CausalViolation {
	clock := map[string][]int{}
	for _, name := range s.req.Names {
		clock[name] = make([]int, s.n)
	}
	var viols []CausalViolation
	for _, d := range s.deliveries {
		senderName := senderOf(d.MsgID, s.req.Names)
		sender := s.nameIndex(senderName)
		cur := clock[d.Node]
		msg, ok := s.registry[d.MsgID]
		if !ok || sender < 0 {
			viols = append(viols, CausalViolation{Node: d.Node, MsgID: d.MsgID, Detail: "unknown message"})
			continue
		}
		if !node.Deliverable(node.VC(cur), msg.Clock, sender) {
			viols = append(viols, CausalViolation{
				Node: d.Node, MsgID: d.MsgID,
				Detail: "delivered while vector-clock dependencies were unsatisfied",
			})
		}
		node.VC(cur).Merge(msg.Clock)
	}
	return viols
}

func senderOf(msgID string, names []string) string {
	// IDs are "<name><seq>"; pick the longest name that is a prefix so
	// multi-character node names work.
	best := ""
	for _, n := range names {
		if len(msgID) > len(n) && msgID[:len(n)] == n && len(n) > len(best) {
			best = n
		}
	}
	return best
}

func sortReports(r []MissingReport) {
	sort.Slice(r, func(i, j int) bool {
		if r[i].Node != r[j].Node {
			return r[i].Node < r[j].Node
		}
		return r[i].MsgID < r[j].MsgID
	})
}

func sortBlocked(r []BlockedReport) {
	sort.Slice(r, func(i, j int) bool {
		if r[i].Node != r[j].Node {
			return r[i].Node < r[j].Node
		}
		return r[i].MsgID < r[j].MsgID
	})
}
