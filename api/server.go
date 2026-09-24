// Package api wires the core cluster to HTTP: one controller server plus one
// HTTP server per node (A, B), all on real TCP loopback listeners.
//
// Disconnects are simulated deterministically at the controller's link table
// (see core.SetLink): a partitioned node neither receives transferred batches
// nor can confirm writes, exactly as if the network were cut.
package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"

	"shardmig/core"
)

// System is the running simulator: controller + two node HTTP servers.
type System struct {
	Cluster *core.Cluster

	controllerLn net.Listener
	nodeLn       map[string]net.Listener
	servers      []*http.Server
	urls         map[string]string

	closeOnce sync.Once
}

// New starts all three HTTP servers. The controller binds controllerAddr
// (e.g. "127.0.0.1:8080"); pass "" for an ephemeral port. Nodes always use
// ephemeral loopback ports.
func New(c *core.Cluster, controllerAddr string) (*System, error) {
	s := &System{
		Cluster: c,
		nodeLn:  map[string]net.Listener{},
		urls:    map[string]string{},
	}

	if controllerAddr == "" {
		controllerAddr = "127.0.0.1:0"
	}
	ctlLn, err := net.Listen("tcp", controllerAddr)
	if err != nil {
		return nil, err
	}
	s.controllerLn = ctlLn
	s.urls["controller"] = "http://" + ctlLn.Addr().String()

	for _, n := range []string{core.NodeA, core.NodeB} {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			s.Close()
			return nil, err
		}
		s.nodeLn[n] = ln
		s.urls[n] = "http://" + ln.Addr().String()
	}

	s.servers = []*http.Server{
		{Handler: s.controllerMux()},
		{Handler: s.nodeMux(core.NodeA)},
		{Handler: s.nodeMux(core.NodeB)},
	}
	go s.servers[0].Serve(ctlLn)
	go s.servers[1].Serve(s.nodeLn[core.NodeA])
	go s.servers[2].Serve(s.nodeLn[core.NodeB])

	return s, nil
}

// ControllerURL is the base URL clients use for writes and control messages.
func (s *System) ControllerURL() string { return s.urls["controller"] }

// NodeURL returns one node's base URL.
func (s *System) NodeURL(n string) string { return s.urls[n] }

// Close stops all HTTP servers.
func (s *System) Close() {
	s.closeOnce.Do(func() {
		for _, srv := range s.servers {
			srv.Close()
		}
		if s.controllerLn != nil {
			s.controllerLn.Close()
		}
		for _, ln := range s.nodeLn {
			ln.Close()
		}
	})
}

// ---- json helpers ------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

func errBody(code, msg string) map[string]any {
	return map[string]any{"error": msg, "code": code}
}

// mapCoreError translates a core sentinel to an HTTP status + stable code.
func mapCoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, core.ErrStaleEpoch):
		writeJSON(w, http.StatusConflict, errBody("stale_epoch", err.Error()))
	case errors.Is(err, core.ErrNotPrimary):
		writeJSON(w, http.StatusForbidden, errBody("not_primary", err.Error()))
	case errors.Is(err, core.ErrClusterFrozen):
		writeJSON(w, http.StatusServiceUnavailable, errBody("cluster_frozen", err.Error()))
	case errors.Is(err, core.ErrNodeDown):
		writeJSON(w, http.StatusBadGateway, errBody("node_down", err.Error()))
	case errors.Is(err, core.ErrBadPhase):
		writeJSON(w, http.StatusConflict, errBody("bad_phase", err.Error()))
	case errors.Is(err, core.ErrUnknownNode):
		writeJSON(w, http.StatusNotFound, errBody("unknown_node", err.Error()))
	default:
		writeJSON(w, http.StatusInternalServerError, errBody("internal", err.Error()))
	}
}

// ---- controller mux -----------------------------------------------------------

func (s *System) controllerMux() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true})
	})

	mux.HandleFunc("GET /status", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, s.statusBody())
	})

	mux.HandleFunc("GET /route", func(w http.ResponseWriter, r *http.Request) {
		st := s.Cluster.Snapshot()
		writeJSON(w, 200, map[string]any{
			"epoch":     st.Epoch,
			"primary":   st.Primary,
			"phase":     st.Phase,
			"write_url": s.ControllerURL() + "/append",
			"node_url":  s.urls[st.Primary],
		})
	})

	mux.HandleFunc("POST /append", s.handleAppend)
	mux.HandleFunc("POST /migration/{action}", s.handleMigration)
	mux.HandleFunc("POST /nodes/{node}/link", s.handleSetLink)
	mux.HandleFunc("POST /nodes/{node}/drop-append", s.handleDropAppend)
	mux.HandleFunc("POST /control/reset", func(w http.ResponseWriter, r *http.Request) {
		s.Cluster.Reset()
		writeJSON(w, 200, s.statusBody())
	})
	mux.HandleFunc("GET /verify", func(w http.ResponseWriter, r *http.Request) {
		checks := s.Cluster.Verify()
		allPass := true
		for _, c := range checks {
			if !c.Pass {
				allPass = false
			}
		}
		writeJSON(w, 200, map[string]any{"all_pass": allPass, "checks": checks})
	})

	return mux
}

func (s *System) statusBody() map[string]any {
	st := s.Cluster.Snapshot()
	return map[string]any{
		"epoch":        st.Epoch,
		"primary":      st.Primary,
		"phase":        st.Phase,
		"snapshot_seq": st.SnapshotSeq,
		"last_seq":     st.LastSeq,
		"links_up":     st.LinksUp,
		"nodes": map[string]any{
			core.NodeA: map[string]any{
				"epoch": st.Nodes[0].NodeEpoch, "is_primary": st.Nodes[0].IsPrimary,
				"write_count": st.Nodes[0].WriteCount, "last_seq": st.Nodes[0].LastSeq,
				"url": s.urls[core.NodeA],
			},
			core.NodeB: map[string]any{
				"epoch": st.Nodes[1].NodeEpoch, "is_primary": st.Nodes[1].IsPrimary,
				"write_count": st.Nodes[1].WriteCount, "last_seq": st.Nodes[1].LastSeq,
				"url": s.urls[core.NodeB],
			},
		},
	}
}

type appendReq struct {
	Node  string `json:"node"`
	Epoch int    `json:"epoch"`
	Key   string `json:"key"`
	Value string `json:"value"`
}

func (s *System) handleAppend(w http.ResponseWriter, r *http.Request) {
	var req appendReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, errBody("bad_json", err.Error()))
		return
	}
	if req.Node == "" {
		req.Node = s.Cluster.Snapshot().Primary
	}
	res, dropped, err := s.Cluster.Append(req.Node, req.Epoch, req.Key, req.Value)
	if err != nil {
		mapCoreError(w, err)
		return
	}
	if dropped {
		// Injected fault: the write is committed but no response is delivered.
		writeJSON(w, 500, errBody("response_dropped_after_commit",
			"write committed on the primary but the response was lost (injected)"))
		return
	}
	writeJSON(w, 200, res)
}

type ctrlReq struct {
	CommandID string `json:"command_id"`
}

// handleMigration executes one phase-control command. Snapshot and catch-up
// batches are transferred over real HTTP from A to B before the idempotent
// core commit, so a partitioned node blocks data movement as well as writes.
func (s *System) handleMigration(w http.ResponseWriter, r *http.Request) {
	action := r.PathValue("action")
	var req ctrlReq
	_ = json.NewDecoder(r.Body).Decode(&req) // body optional

	var res core.CtrlResult
	var err error
	switch action {
	case "begin_snapshot":
		res, err = s.Cluster.BeginSnapshot(req.CommandID)
	case "complete_snapshot":
		res, err = s.completeSnapshot(req.CommandID)
	case "catch_up":
		res, err = s.catchUp(req.CommandID)
	case "prepare_cutover":
		res, err = s.Cluster.PrepareCutover(req.CommandID)
	case "commit_cutover":
		res, err = s.Cluster.CommitCutover(req.CommandID)
	default:
		writeJSON(w, 404, errBody("unknown_action", "unknown migration action: "+action))
		return
	}
	if err != nil {
		mapCoreError(w, err)
		return
	}
	body := res.Body
	body["status"] = s.statusBody()
	if res.Replayed {
		w.Header().Set("X-Replayed-Command", "1")
	}
	writeJSON(w, 200, body)
}

// fetchWrites pulls writes from A's node server over HTTP.
func (s *System) fetchWrites(after, upTo int64) ([]*core.Write, error) {
	u := s.urls[core.NodeA] + "/internal/log?after=" + strconv.FormatInt(after, 10) +
		"&upto=" + strconv.FormatInt(upTo, 10)
	resp, err := http.Post(u, "application/json", nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("fetching A log: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Writes []*core.Write `json:"writes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out.Writes, nil
}

// installWrites pushes a batch onto B's node server over HTTP and returns how
// many new writes B actually applied (retransmitted duplicates count as 0).
func (s *System) installWrites(batch []*core.Write) (int, error) {
	body, _ := json.Marshal(map[string]any{"writes": batch})
	resp, err := http.Post(s.urls[core.NodeB]+"/internal/install", "application/json",
		strings.NewReader(string(body)))
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, fmt.Errorf("installing on B: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Installed int `json:"installed"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, err
	}
	return out.Installed, nil
}

func (s *System) completeSnapshot(cmdID string) (core.CtrlResult, error) {
	st := s.Cluster.Snapshot()
	if !st.LinksUp[core.NodeA] {
		return core.CtrlResult{}, fmt.Errorf("read snapshot from A: %w", core.ErrNodeDown)
	}
	if !st.LinksUp[core.NodeB] {
		return core.CtrlResult{}, fmt.Errorf("install snapshot on B: %w", core.ErrNodeDown)
	}
	batch, err := s.fetchWrites(0, st.SnapshotSeq)
	if err != nil {
		return core.CtrlResult{}, err
	}
	installed, err := s.installWrites(batch)
	if err != nil {
		return core.CtrlResult{}, err
	}
	return s.Cluster.CompleteSnapshot(cmdID, batch, installed)
}

func (s *System) catchUp(cmdID string) (core.CtrlResult, error) {
	st := s.Cluster.Snapshot()
	if !st.LinksUp[core.NodeA] {
		return core.CtrlResult{}, fmt.Errorf("read tail from A: %w", core.ErrNodeDown)
	}
	if !st.LinksUp[core.NodeB] {
		return core.CtrlResult{}, fmt.Errorf("install tail on B: %w", core.ErrNodeDown)
	}
	bLast := int64(0)
	for _, n := range st.Nodes {
		if n.Name == core.NodeB {
			bLast = n.LastSeq
		}
	}
	batch, err := s.fetchWrites(bLast, -1)
	if err != nil {
		return core.CtrlResult{}, err
	}
	installed, err := s.installWrites(batch)
	if err != nil {
		return core.CtrlResult{}, err
	}
	return s.Cluster.CatchUp(cmdID, batch, installed)
}

type linkReq struct {
	Up bool `json:"up"`
}

func (s *System) handleSetLink(w http.ResponseWriter, r *http.Request) {
	node := r.PathValue("node")
	var req linkReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, errBody("bad_json", err.Error()))
		return
	}
	if err := s.Cluster.SetLink(node, req.Up); err != nil {
		mapCoreError(w, err)
		return
	}
	writeJSON(w, 200, s.statusBody())
}

type dropReq struct {
	Drop bool `json:"drop"`
}

func (s *System) handleDropAppend(w http.ResponseWriter, r *http.Request) {
	node := r.PathValue("node")
	var req dropReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, errBody("bad_json", err.Error()))
		return
	}
	if err := s.Cluster.SetDropAppend(node, req.Drop); err != nil {
		mapCoreError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"node": node, "drop_append": req.Drop})
}

// ---- node mux -----------------------------------------------------------------

func (s *System) nodeMux(node string) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true, "node": node})
	})

	mux.HandleFunc("GET /info", func(w http.ResponseWriter, r *http.Request) {
		ep, _ := s.Cluster.NodeEpoch(node)
		st := s.Cluster.Snapshot()
		writeJSON(w, 200, map[string]any{
			"node":       node,
			"node_epoch": ep,
			"is_primary": st.Primary == node && st.Epoch == ep,
			"phase":      st.Phase,
		})
	})

	mux.HandleFunc("GET /writes", func(w http.ResponseWriter, r *http.Request) {
		log, err := s.Cluster.NodeLog(node)
		if err != nil {
			mapCoreError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"node": node, "writes": log})
	})

	// Internal: controller transfers log ranges over real HTTP.
	mux.HandleFunc("POST /internal/log", func(w http.ResponseWriter, r *http.Request) {
		after, _ := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
		upTo, _ := strconv.ParseInt(r.URL.Query().Get("upto"), 10, 64)
		batch, err := s.Cluster.LogRange(node, after, upTo)
		if err != nil {
			mapCoreError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"writes": batch})
	})

	mux.HandleFunc("POST /internal/install", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Writes []*core.Write `json:"writes"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, 400, errBody("bad_json", err.Error()))
			return
		}
		installed, last := s.Cluster.InstallOnB(req.Writes)
		writeJSON(w, 200, map[string]any{"installed": installed, "b_last_seq": last})
	})

	return mux
}
