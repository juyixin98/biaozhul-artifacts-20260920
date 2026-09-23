// Command server exposes the deterministic Raft simulator over HTTP using
// only the standard library (net/http). It is a control plane for the lab:
// drive ticks, inject faults (partitions, restarts, stale-message release),
// inspect node/cluster state, replay JSON fault traces, and run the bounded
// enumeration. It is NOT a production Raft service: all nodes live in one
// process and talk over an in-memory network.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"raftlab/internal/check"
	"raftlab/internal/raft"
	"raftlab/internal/sim"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	size := flag.Int("nodes", 3, "cluster size (3-5)")
	seed := flag.Int64("seed", 42, "deterministic RNG seed")
	tickMS := flag.Int("tick-ms", 100, "auto-tick period in milliseconds; 0 = manual stepping")
	dataDir := flag.String("data-dir", "", "persist node state as JSON under this dir (default: in-memory)")
	buggy := flag.Bool("buggy", false, "enable the Figure-8 old-term-commit bug (for counterexample demos)")
	flag.Parse()

	if *size < 1 || *size > 5 {
		fmt.Fprintln(os.Stderr, "-nodes must be between 1 and 5")
		os.Exit(2)
	}

	cfg := sim.ClusterConfig{
		Size: *size, ElectionMin: 8, ElectionMax: 15,
		Heartbeat: 4, Latency: 1, Seed: *seed,
		BuggyOldTermCommit: *buggy,
	}
	if *dataDir != "" {
		cfg.Storage = func(id int) raft.Storage {
			return raft.NewFileStorage(fmt.Sprintf("%s/node%d", *dataDir, id))
		}
	}
	cl, err := sim.NewCluster(cfg)
	if err != nil {
		log.Fatal(err)
	}
	if *tickMS > 0 {
		cl.SetAutoTick(true, time.Duration(*tickMS)*time.Millisecond)
	}
	defer cl.Close()

	srv := &server{
		cl:      cl,
		cluster: cfg,
		buggy:   *buggy,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/cluster", srv.getCluster)
	mux.HandleFunc("GET /v1/nodes", srv.getNodes)
	mux.HandleFunc("GET /v1/nodes/{id}", srv.getNode)
	mux.HandleFunc("POST /v1/tick", srv.postTick)
	mux.HandleFunc("POST /v1/propose", srv.postPropose)
	mux.HandleFunc("POST /v1/nodes/{id}/stop", srv.postStop)
	mux.HandleFunc("POST /v1/nodes/{id}/start", srv.postStart)
	mux.HandleFunc("POST /v1/nodes/{id}/restart", srv.postRestart)
	mux.HandleFunc("POST /v1/nodes/{id}/partition", srv.postPartition)
	mux.HandleFunc("POST /v1/heal", srv.postHeal)
	mux.HandleFunc("POST /v1/stale/release", srv.postReleaseStale)
	mux.HandleFunc("POST /v1/stale/drop", srv.postDropStale)
	mux.HandleFunc("POST /v1/replay", srv.postReplay)
	mux.HandleFunc("POST /v1/enumerate", srv.postEnumerate)
	mux.HandleFunc("GET /v1/figure8", srv.getFigure8)
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})

	log.Printf("raft lab listening on %s (nodes=%d seed=%d tickMS=%d buggy=%v dataDir=%q)",
		*addr, *size, *seed, *tickMS, *buggy, *dataDir)
	log.Fatal(http.ListenAndServe(*addr, logRequests(mux)))
}

type server struct {
	cl      *sim.Cluster
	cluster sim.ClusterConfig
	buggy   bool
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func (s *server) getCluster(w http.ResponseWriter, r *http.Request) {
	id, term, ok := s.cl.Leader()
	writeJSON(w, http.StatusOK, map[string]any{
		"tick":       s.cl.Tick(),
		"leaderId":   id,
		"leaderTerm": term,
		"hasLeader":  ok,
		"stale":      s.cl.StaleCount(),
		"nodes":      s.cl.IDs(),
		"buggy":      s.buggy,
		"nodeStates": s.cl.Nodes(),
	})
}

func (s *server) getNodes(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.cl.Nodes())
}

func (s *server) getNode(w http.ResponseWriter, r *http.Request) {
	id, ok := nodeID(w, r)
	if !ok {
		return
	}
	snap := s.cl.Node(id)
	writeJSON(w, http.StatusOK, map[string]any{
		"snapshot": snap,
		"kv":       s.cl.KV(id),
	})
}

func (s *server) postTick(w http.ResponseWriter, r *http.Request) {
	var body struct {
		N int `json:"n"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	if body.N < 1 {
		body.N = 1
	}
	s.cl.Run(body.N)
	if v := check.CheckInvariants(s.cl, -1); v != nil {
		writeJSON(w, http.StatusConflict, map[string]any{
			"ok":        false,
			"tick":      s.cl.Tick(),
			"violation": v,
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "tick": s.cl.Tick()})
}

func (s *server) postPropose(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Node    int    `json:"node"`
		Command string `json:"command"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if body.Command == "" {
		writeErr(w, http.StatusBadRequest, "command is required, e.g. \"SET k v\"")
		return
	}
	var accepted bool
	var leader int
	if body.Node > 0 {
		accepted = s.cl.Propose(body.Node, body.Command)
		leader = body.Node
	} else {
		leader, accepted = s.cl.ProposeLeader(body.Command)
	}
	status := http.StatusOK
	if !accepted {
		status = http.StatusConflict
	}
	writeJSON(w, status, map[string]any{"accepted": accepted, "node": leader})
}

func (s *server) postStop(w http.ResponseWriter, r *http.Request) {
	id, ok := nodeID(w, r)
	if !ok {
		return
	}
	s.cl.Stop(id)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "node": id, "alive": false})
}

func (s *server) postStart(w http.ResponseWriter, r *http.Request) {
	id, ok := nodeID(w, r)
	if !ok {
		return
	}
	if err := s.cl.Start(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "node": id, "alive": true})
}

func (s *server) postRestart(w http.ResponseWriter, r *http.Request) {
	id, ok := nodeID(w, r)
	if !ok {
		return
	}
	s.cl.Stop(id)
	if err := s.cl.Start(id); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "node": id})
}

func (s *server) postPartition(w http.ResponseWriter, r *http.Request) {
	id, ok := nodeID(w, r)
	if !ok {
		return
	}
	var body struct {
		Group string `json:"group"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	if body.Group != "" {
		s.cl.SetGroup(id, body.Group)
	} else {
		s.cl.Partition(id)
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "node": id, "group": body.Group})
}

func (s *server) postHeal(w http.ResponseWriter, r *http.Request) {
	s.cl.Heal()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) postReleaseStale(w http.ResponseWriter, r *http.Request) {
	n := s.cl.ReleaseStale()
	writeJSON(w, http.StatusOK, map[string]any{"released": n})
}

func (s *server) postDropStale(w http.ResponseWriter, r *http.Request) {
	n := s.cl.DropStale()
	writeJSON(w, http.StatusOK, map[string]any{"dropped": n})
}

// replayRequest runs an arbitrary JSON action trace against a FRESH cluster
// with the server's config, so counterexamples reproduce deterministically
// without touching the server's running demo cluster.
type replayRequest struct {
	Preset    string         `json:"preset,omitempty"` // "figure8" uses the deterministic 5-node Figure 8 timing
	Seed      int64          `json:"seed,omitempty"`
	Size      int            `json:"size,omitempty"`
	Buggy     *bool          `json:"buggy,omitempty"`
	Actions   []check.Action `json:"actions"`
	AutoCheck bool           `json:"autoCheck,omitempty"` // check invariants after every tick
}

func (s *server) postReplay(w http.ResponseWriter, r *http.Request) {
	var req replayRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(req.Actions) == 0 {
		writeErr(w, http.StatusBadRequest, "actions[] is required")
		return
	}
	cfg := s.cluster
	buggy := cfg.BuggyOldTermCommit
	if req.Buggy != nil {
		buggy = *req.Buggy
	}
	if req.Preset == "figure8" {
		cfg = check.Figure8Config(buggy)
	} else {
		if req.Seed != 0 {
			cfg.Seed = req.Seed
		}
		if req.Size > 0 {
			cfg.Size = req.Size
		}
		cfg.BuggyOldTermCommit = buggy
	}
	cfg.Storage = nil // replays always use in-memory storage
	rr := check.NewRunner(cfg)
	cl, res := rr.Run(req.Actions)
	resp := map[string]any{
		"ok":        res.OK(),
		"finalTick": res.FinalTick,
		"leaderId":  res.Leader,
		"nodes":     cl.Nodes(),
	}
	if res.Violation != nil {
		resp["violation"] = res.Violation
		writeJSON(w, http.StatusConflict, resp)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *server) postEnumerate(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Size         int   `json:"size"`
		Seed         int64 `json:"seed"`
		MaxDepth     int   `json:"maxDepth"`
		MaxTraces    int   `json:"maxTraces"`
		TicksPerStep int   `json:"ticksPerStep"`
		Buggy        bool  `json:"buggy"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)
	cfg := check.Default3NodeConfig()
	if body.Size > 0 {
		cfg.Size = body.Size
	}
	if body.Seed != 0 {
		cfg.Seed = body.Seed
	}
	cfg.BuggyOldTermCommit = body.Buggy
	ecfg := check.EnumerationConfig{
		Cluster:      cfg,
		MaxDepth:     body.MaxDepth,
		MaxTraces:    body.MaxTraces,
		TicksPerStep: body.TicksPerStep,
	}
	if body.Buggy && body.Size == 5 {
		seed, err := check.Figure8Actions(true)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, err.Error())
			return
		}
		ecfg.Cluster = check.Figure8Config(true)
		ecfg.Seeds = [][]check.Action{seed}
	}
	rep := check.Enumerate(ecfg)
	status := http.StatusOK
	if len(rep.Violations) > 0 {
		status = http.StatusConflict
	}
	writeJSON(w, status, rep)
}

func (s *server) getFigure8(w http.ResponseWriter, r *http.Request) {
	buggy := r.URL.Query().Get("buggy") == "true"
	actions, err := check.Figure8Actions(buggy)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	rr := check.NewRunner(check.Figure8Config(buggy))
	_, res := rr.Run(actions)
	resp := map[string]any{
		"buggy":  buggy,
		"ok":     res.OK(),
		"trace":  actions,
		"result": res,
	}
	status := http.StatusOK
	if res.Violation != nil {
		status = http.StatusConflict
	}
	writeJSON(w, status, resp)
}

func nodeID(w http.ResponseWriter, r *http.Request) (int, bool) {
	v := r.PathValue("id")
	var id int
	if _, err := fmt.Sscanf(v, "%d", &id); err != nil || id < 1 {
		writeErr(w, http.StatusBadRequest, "node id must be a positive integer")
		return 0, false
	}
	return id, true
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
		log.Printf("%s %s", r.Method, r.URL.RequestURI())
	})
}
