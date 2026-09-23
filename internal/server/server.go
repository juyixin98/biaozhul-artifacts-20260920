// Package server hosts an in-process cluster of simulated replicas and
// exposes each over HTTP under /replicas/{id}/...
//
// All replicas live in one process purely to make the simulation easy to run
// and test; they share no data. Messages only move between them through the
// explicit sync endpoints, exactly as they would over a network.
package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"sync"

	"vcreg/internal/register"
	"vcreg/internal/vclock"
)

// Cluster is a set of named replicas with independent stores.
type Cluster struct {
	mu       sync.Mutex
	replicas map[string]*register.Store
}

// NewCluster creates an empty cluster.
func NewCluster() *Cluster {
	return &Cluster{replicas: make(map[string]*register.Store)}
}

// Ensure creates the replica if missing and returns its store.
func (c *Cluster) Ensure(id string) *register.Store {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.replicas[id]
	if !ok {
		s = register.NewStore(id)
		c.replicas[id] = s
	}
	return s
}

// get returns the store or nil when the replica is unknown.
func (c *Cluster) get(id string) *register.Store {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.replicas[id]
}

// IDs returns the sorted replica ids.
func (c *Cluster) IDs() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, 0, len(c.replicas))
	for id := range c.replicas {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// versionDTO is the wire form of register.Version.
type versionDTO struct {
	Value  string           `json:"value"`
	Clock  map[string]int64 `json:"clock"`
	Origin string           `json:"origin"`
}

func toDTO(v register.Version) versionDTO {
	c := map[string]int64{}
	for k, n := range v.Clock {
		c[k] = n
	}
	return versionDTO{Value: v.Value, Clock: c, Origin: v.Origin}
}

func toVersions(vs []register.Version) []versionDTO {
	out := make([]versionDTO, 0, len(vs))
	for _, v := range vs {
		out = append(out, toDTO(v))
	}
	return out
}

// writeJSON writes v as JSON with the given status.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// Handler builds the HTTP handler for the cluster.
func Handler(c *Cluster) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /replicas", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"replicas": c.IDs()})
	})

	mux.HandleFunc("PUT /replicas/{rid}", func(w http.ResponseWriter, r *http.Request) {
		rid := r.PathValue("rid")
		c.Ensure(rid)
		writeJSON(w, http.StatusOK, map[string]string{"replica": rid, "status": "ready"})
	})

	mux.HandleFunc("GET /replicas/{rid}/keys", func(w http.ResponseWriter, r *http.Request) {
		s := c.get(r.PathValue("rid"))
		if s == nil {
			writeErr(w, http.StatusNotFound, "unknown replica")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"keys": s.Keys()})
	})

	mux.HandleFunc("GET /replicas/{rid}/keys/{key}", func(w http.ResponseWriter, r *http.Request) {
		s := c.get(r.PathValue("rid"))
		if s == nil {
			writeErr(w, http.StatusNotFound, "unknown replica")
			return
		}
		vs := s.Read(r.PathValue("key"))
		writeJSON(w, http.StatusOK, map[string]any{
			"key":      r.PathValue("key"),
			"replica":  r.PathValue("rid"),
			"siblings": toVersions(vs),
			"count":    len(vs),
		})
	})

	mux.HandleFunc("PUT /replicas/{rid}/keys/{key}", func(w http.ResponseWriter, r *http.Request) {
		s := c.get(r.PathValue("rid"))
		if s == nil {
			writeErr(w, http.StatusNotFound, "unknown replica")
			return
		}
		var body struct {
			Value string           `json:"value"`
			Clock map[string]int64 `json:"clock"` // optional: explicit parent clock
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		parent := vclock.Clock(body.Clock)
		v := s.Write(r.PathValue("key"), body.Value, parent)
		writeJSON(w, http.StatusOK, map[string]any{
			"status":  "written",
			"version": toDTO(v),
		})
	})

	// Explicit, context-bearing merge.
	mux.HandleFunc("POST /replicas/{rid}/keys/{key}/resolve", func(w http.ResponseWriter, r *http.Request) {
		s := c.get(r.PathValue("rid"))
		if s == nil {
			writeErr(w, http.StatusNotFound, "unknown replica")
			return
		}
		var body struct {
			Value   string             `json:"value"`
			Context []map[string]int64 `json:"context"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		ctx := make([]vclock.Clock, 0, len(body.Context))
		for _, m := range body.Context {
			ctx = append(ctx, vclock.Clock(m))
		}
		v, err := s.Resolve(r.PathValue("key"), body.Value, ctx)
		if err != nil {
			writeErr(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"status":  "resolved",
			"version": toDTO(v),
		})
	})

	// One-way message delivery: merge one version into a replica.
	mux.HandleFunc("POST /replicas/{rid}/receive/{key}", func(w http.ResponseWriter, r *http.Request) {
		s := c.get(r.PathValue("rid"))
		if s == nil {
			writeErr(w, http.StatusNotFound, "unknown replica")
			return
		}
		var dto versionDTO
		if err := json.NewDecoder(r.Body).Decode(&dto); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		in := register.Version{Value: dto.Value, Origin: dto.Origin, Clock: vclock.Clock(dto.Clock)}
		admitted := s.Receive(r.PathValue("key"), in)
		status := "admitted"
		if !admitted {
			status = "ignored_stale_or_duplicate"
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"status":   status,
			"admitted": admitted,
			"siblings": toVersions(s.Read(r.PathValue("key"))),
		})
	})

	// Gossip-style full snapshot sync: pull everything from src into dst.
	mux.HandleFunc("POST /sync", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			From string `json:"from"`
			To   string `json:"to"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		src, dst := c.get(body.From), c.get(body.To)
		if src == nil || dst == nil {
			writeErr(w, http.StatusNotFound,
				fmt.Sprintf("unknown replica: from=%q to=%q", body.From, body.To))
			return
		}
		snap := src.Snapshot()
		admitted := dst.MergeSnapshot(snap)
		writeJSON(w, http.StatusOK, map[string]any{
			"from":              body.From,
			"to":                body.To,
			"versions_admitted": admitted,
			"to_snapshot":       toSnapshotDTO(dst),
		})
	})

	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeErr(w, http.StatusNotFound, "no such path")
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprint(w, routesHelp)
	})

	return mux
}

type snapshotWire struct {
	Replica string                  `json:"replica"`
	Data    map[string][]versionDTO `json:"data"`
}

func toSnapshotDTO(s *register.Store) snapshotWire {
	snap := s.Snapshot()
	dto := snapshotWire{Replica: snap.Replica, Data: map[string][]versionDTO{}}
	for k, vs := range snap.Data {
		dto.Data[k] = toVersions(vs)
	}
	return dto
}

const routesHelp = `Vector-clock multi-version register.

  GET    /replicas
  PUT    /replicas/{rid}
  GET    /replicas/{rid}/keys
  GET    /replicas/{rid}/keys/{key}
  PUT    /replicas/{rid}/keys/{key}            {"value":"...", "clock":{...optional parent...}}
  POST   /replicas/{rid}/keys/{key}/resolve    {"value":"...", "context":[{clock},...]}
  POST   /replicas/{rid}/receive/{key}         {"value":"...", "clock":{...}, "origin":"..."}
  POST   /sync                                 {"from":"a", "to":"b"}
`
