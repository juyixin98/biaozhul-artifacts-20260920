// Package httpx exposes the simulated cluster over a small JSON HTTP API.
// It is a demo/teaching surface: sessions live in process memory, there is no
// persistence of sessions and no production networking.
package httpx

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"

	"raftlab/raft"
	"raftlab/verify"
)

// Server holds in-memory simulation sessions keyed by id.
type Server struct {
	mu       sync.Mutex
	sessions map[string]*session
	nextID   int
}

type session struct {
	sim     *raft.Simulator
	nodes   int
	variant string
	seed    int64
}

// New creates an empty server.
func New() *Server {
	return &Server{sessions: map[string]*session{}}
}

// Routes registers the JSON API on mux.
func (s *Server) Routes(mux *http.ServeMux) {
	mux.HandleFunc("/sessions", s.handleSessions)
	mux.HandleFunc("/sessions/", s.handleSession)
	mux.HandleFunc("/scenarios/run", s.handleScenarioRun)
	mux.HandleFunc("/scenarios/counterexamples", s.handleCounterexamples)
	mux.HandleFunc("/enumerate", s.handleEnumerate)
}

func writeJSON(w http.ResponseWriter, code int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, code int, format string, args ...interface{}) {
	writeJSON(w, code, map[string]string{"error": fmt.Sprintf(format, args...)})
}

type createReq struct {
	Nodes   int    `json:"nodes"`
	Variant string `json:"variant"`
	Seed    int64  `json:"seed"`
	Trace   bool   `json:"trace"`
}

func (s *Server) handleSessions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST required")
		return
	}
	var req createReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: %v", err)
		return
	}
	if req.Nodes == 0 {
		req.Nodes = 3
	}
	if req.Nodes < 3 || req.Nodes > 5 {
		writeErr(w, http.StatusBadRequest, "nodes must be 3..5")
		return
	}
	variant := raft.StandardVariant()
	switch req.Variant {
	case "", "standard":
		req.Variant = "standard"
	case "naive":
		variant = raft.NaiveVariant()
	case "notermcheck":
		variant = raft.StaleAppendVariant()
	default:
		writeErr(w, http.StatusBadRequest, "unknown variant %q", req.Variant)
		return
	}

	ids := make([]int, req.Nodes)
	for i := range ids {
		ids[i] = i + 1
	}
	s.mu.Lock()
	s.nextID++
	id := fmt.Sprintf("s%d", s.nextID)
	sim := raft.NewSimulator(ids, variant, req.Seed)
	if req.Trace {
		sim.EnableTrace()
	}
	s.sessions[id] = &session{sim: sim, nodes: req.Nodes, variant: req.Variant, seed: req.Seed}
	s.mu.Unlock()

	writeJSON(w, http.StatusCreated, map[string]interface{}{
		"id": id, "nodes": req.Nodes, "variant": req.Variant, "seed": req.Seed,
	})
}

func (s *Server) get(id string) (*session, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess, ok := s.sessions[id]
	return sess, ok
}

func (s *Server) handleSession(w http.ResponseWriter, r *http.Request) {
	// paths: /sessions/{id} and /sessions/{id}/{action}
	path := r.URL.Path
	rest := path[len("/sessions/"):]
	id := rest
	action := ""
	if i := indexByte(rest, '/'); i >= 0 {
		id = rest[:i]
		action = rest[i+1:]
	}
	sess, ok := s.get(id)
	if !ok {
		writeErr(w, http.StatusNotFound, "no such session %q", id)
		return
	}
	if action == "" {
		sess.sim.MuLock()
		s.writeSnapshot(w, sess)
		sess.sim.MuUnlock()
		return
	}
	s.action(w, r, sess, action)
}

func indexByte(s string, b byte) int {
	for i := 0; i < len(s); i++ {
		if s[i] == b {
			return i
		}
	}
	return -1
}

// writeSnapshot emits cluster state. Caller must hold sess.sim's lock.
func (s *Server) writeSnapshot(w http.ResponseWriter, sess *session) {
	writeJSON(w, 200, map[string]interface{}{
		"now":    sess.sim.Now(),
		"leader": sess.sim.Leader(),
		"nodes":  sess.sim.Views(),
		"trace":  sess.sim.TraceCopy(),
	})
}

type advanceReq struct {
	Ms int64 `json:"ms"`
}

type partitionReq struct {
	Groups  [][]int `json:"groups"`
	Isolate []int   `json:"isolate"`
}

type nodeReq struct {
	Node int `json:"node"`
}

type proposeReq struct {
	Node    int    `json:"node"` // optional: when 0, use current leader
	Command string `json:"command"`
}

type staleReq struct {
	From    int    `json:"from"`
	To      int    `json:"to"`
	Term    int    `json:"term"`
	Index   int    `json:"index"`
	Command string `json:"command"`
}

func (s *Server) action(w http.ResponseWriter, r *http.Request, sess *session, action string) {
	decode := func(v interface{}) bool {
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(v)
		}
		return true
	}
	sess.sim.MuLock()
	defer sess.sim.MuUnlock()

	switch action {
	case "advance":
		var req advanceReq
		decode(&req)
		if req.Ms <= 0 {
			req.Ms = 100
		}
		sess.sim.Advance(req.Ms*1_000_000, nil)
	case "partition":
		var req partitionReq
		decode(&req)
		if len(req.Groups) > 0 {
			sess.sim.Network().Partition(req.Groups)
		} else if len(req.Isolate) > 0 {
			sess.sim.Network().Partition(isolateGroups(sess.sim.IDs(), req.Isolate))
		}
	case "heal":
		sess.sim.Network().Heal()
	case "pause":
		var req nodeReq
		decode(&req)
		sess.sim.Pause(req.Node)
	case "resume":
		var req nodeReq
		decode(&req)
		sess.sim.Resume(req.Node)
	case "pause-from":
		var req nodeReq
		decode(&req)
		sess.sim.PauseFrom(req.Node)
	case "resume-from":
		var req nodeReq
		decode(&req)
		sess.sim.ResumeFrom(req.Node)
	case "restart":
		var req nodeReq
		decode(&req)
		sess.sim.Restart(req.Node)
	case "propose":
		var req proposeReq
		decode(&req)
		node := req.Node
		if node == 0 {
			node = sess.sim.Leader()
		}
		ok := sess.sim.ClientCommand(node, req.Command)
		writeJSON(w, 200, map[string]interface{}{"accepted": ok, "leader": sess.sim.Leader(), "node": node})
		return
	case "inject-stale-append":
		var req staleReq
		decode(&req)
		term := req.Term
		if term == 0 {
			term = 1
		}
		idx := req.Index
		if idx == 0 {
			idx = 1
		}
		sess.sim.InjectMessage(raft.Message{
			Kind: raft.MsgAppendReq, From: req.From, To: req.To, Term: term,
			Append: &raft.AppendEntries{
				Term: term, LeaderID: req.From, PrevLogI: idx - 1, PrevLogT: 0,
				Entries: []raft.LogEntry{{Term: term, Index: idx, Command: req.Command}},
				Commit:  idx,
			},
		})
	default:
		writeErr(w, http.StatusNotFound, "unknown action %q", action)
		return
	}
	s.writeSnapshot(w, sess)
}

func isolateGroups(all, isolated []int) [][]int {
	set := map[int]bool{}
	for _, id := range isolated {
		set[id] = true
	}
	var rest []int
	for _, id := range all {
		if !set[id] {
			rest = append(rest, id)
		}
	}
	groups := [][]int{append([]int(nil), isolated...)}
	if len(rest) > 0 {
		groups = append(groups, rest)
	}
	return groups
}

func (s *Server) handleScenarioRun(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST required")
		return
	}
	var sc verify.Scenario
	if err := json.NewDecoder(r.Body).Decode(&sc); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid scenario json: %v", err)
		return
	}
	res := verify.Run(sc, verify.Options{Trace: true, CaptureViews: true, EpilogueMs: 120})
	writeJSON(w, 200, res)
}

func (s *Server) handleCounterexamples(w http.ResponseWriter, r *http.Request) {
	ces := verify.CounterexampleScenarios()
	out := make([]verify.Scenario, 0, len(ces))
	out = append(out, ces...)
	writeJSON(w, 200, map[string]interface{}{
		"counterexamples": out,
		"note":            "POST any scenario to /scenarios/run; variants: standard, notermcheck, naive",
	})
}

type enumReq struct {
	Variant string `json:"variant"`
	Depth   int    `json:"depth"`
	Fuzz    int    `json:"fuzz"`
	MaxCE   int    `json:"maxCounterexamples"`
}

func (s *Server) handleEnumerate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, http.StatusMethodNotAllowed, "POST required")
		return
	}
	req := enumReq{Variant: "standard", Depth: 2, Fuzz: 300, MaxCE: 5}
	_ = json.NewDecoder(r.Body).Decode(&req)
	rep := verify.Enumerate(req.Variant, req.Depth, req.Fuzz, req.MaxCE)
	writeJSON(w, 200, rep)
}
