// Package api implements the HTTP layer of the topology-aware GPU placement
// service using only the standard library (net/http, encoding/json).
package api

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"topology-aware-gpu-scheduler/internal/solver"
	"topology-aware-gpu-scheduler/internal/topology"
)

// maxBodyBytes bounds request bodies (1 MiB).
const maxBodyBytes = 1 << 20

// ---- request DTOs ---------------------------------------------------------

type deviceDTO struct {
	ID         string `json:"id"`
	MemoryMB   *int64 `json:"memory_mb"`
	UsedMemory int64  `json:"used_memory_mb"`
	NumaNode   *int   `json:"numa_node"`
}

type linkDTO struct {
	A    string `json:"a"`
	B    string `json:"b"`
	Cost int64  `json:"cost"`
}

type taskDTO struct {
	Name             string `json:"name"`
	Replicas         *int   `json:"replicas"`
	MemoryPerReplica *int64 `json:"memory_per_replica_mb"`
	MaxSearchStates  int64  `json:"max_search_states"`
}

type topologyDefaultsDTO struct {
	SameNumaCost  *int64 `json:"same_numa_cost"`
	CrossNumaCost *int64 `json:"cross_numa_cost"`
}

type placementRequest struct {
	Cluster struct {
		Devices  []deviceDTO         `json:"devices"`
		Links    []linkDTO           `json:"links"`
		Defaults topologyDefaultsDTO `json:"default_link_cost"`
	} `json:"cluster"`
	Task taskDTO `json:"task"`
}

// ---- response DTOs --------------------------------------------------------

type placementEntryResp struct {
	ReplicaIndex int    `json:"replica_index"`
	DeviceID     string `json:"device_id"`
	NumaNode     int    `json:"numa_node"`
}

type pairResp struct {
	From      string `json:"from"`
	To        string `json:"to"`
	Cost      int64  `json:"cost"`
	CrossNuma bool   `json:"cross_numa"`
}

type freeMemoryResp struct {
	DeviceID string `json:"device_id"`
	NumaNode int    `json:"numa_node"`
	FreeMB   int64  `json:"free_mb"`
	Eligible bool   `json:"eligible"`
}

type placementResponse struct {
	Status             string               `json:"status"`
	Reason             string               `json:"reason,omitempty"`
	Message            string               `json:"message,omitempty"`
	TaskName           string               `json:"task_name"`
	Replicas           int                  `json:"replicas"`
	MemoryPerReplicaMB int64                `json:"memory_per_replica_mb"`
	Placement          []placementEntryResp `json:"placement,omitempty"`
	TotalCost          int64                `json:"total_communication_cost,omitempty"`
	CrossNumaPairs     int                  `json:"cross_numa_pair_count,omitempty"`
	Pairs              []pairResp           `json:"pairs,omitempty"`
	Rejected           *rejectionDetail     `json:"rejected,omitempty"`
	Diagnostics        diagnostics          `json:"diagnostics"`
}

type rejectionDetail struct {
	RequiredReplicas int              `json:"required_replicas"`
	EligibleDevices  int              `json:"eligible_devices"`
	TotalDevices     int              `json:"total_devices"`
	RequiredFreeMB   int64            `json:"required_free_mb"`
	LargestFreeMB    int64            `json:"largest_free_mb"`
	FreeMemory       []freeMemoryResp `json:"free_memory"`
}

type diagnostics struct {
	Strategy       string           `json:"strategy"`
	StatesExplored int64            `json:"states_explored"`
	Combinations   int64            `json:"combinations_total"`
	FreeMemory     []freeMemoryResp `json:"free_memory"`
}

type errorResponse struct {
	Error  string `json:"error"`
	Reason string `json:"reason,omitempty"`
}

// Handler wires HTTP routes to the solver.
type Handler struct {
	Logger *log.Logger
}

// NewMux registers the service routes on a fresh mux.
func (h *Handler) NewMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/", h.index)
	mux.HandleFunc("/healthz", h.healthz)
	mux.HandleFunc("/api/v1/placement", h.placement)
	return mux
}

func (h *Handler) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeJSON(w, http.StatusNotFound, errorResponse{Error: "not found"})
		return
	}
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service": "topology-aware-gpu-placement",
		"endpoints": map[string]string{
			"health":    "GET  /healthz",
			"placement": "POST /api/v1/placement",
		},
		"description": "Assign GPU task replicas to devices minimizing group-internal communication cost under memory and topology constraints",
	})
}

func (h *Handler) healthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Handler) placement(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed; use POST"})
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeJSON(w, http.StatusRequestEntityTooLarge, errorResponse{Error: "request body too large (limit 1 MiB)"})
			return
		}
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "cannot read body: " + err.Error(), Reason: "INVALID_REQUEST"})
		return
	}

	var req placementRequest
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "invalid JSON: " + err.Error(), Reason: "INVALID_REQUEST"})
		return
	}
	if dec.More() {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: "invalid JSON: unexpected trailing data", Reason: "INVALID_REQUEST"})
		return
	}

	cluster, solverReq, verr := validate(&req)
	if verr != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: verr.Error(), Reason: "INVALID_REQUEST"})
		return
	}

	result, err := solver.Solve(cluster, solverReq)
	if err != nil {
		// Defensive: validation should have caught all of these.
		writeJSON(w, http.StatusBadRequest, errorResponse{Error: err.Error(), Reason: "INVALID_REQUEST"})
		return
	}

	// Both feasible and infeasible results are "the scheduler answered the
	// question": HTTP 200, with status carried in the body.
	writeJSON(w, http.StatusOK, toResponse(result, solverReq.Name))
}

// validate converts DTOs into domain types, returning an aggregated list of
// request problems instead of failing on the first one.
func validate(req *placementRequest) (*topology.Cluster, solver.Request, error) {
	var problems []string

	if len(req.Cluster.Devices) == 0 {
		problems = append(problems, "cluster.devices must contain at least one device")
	}
	if req.Task.Replicas == nil {
		problems = append(problems, "task.replicas is required")
	} else if *req.Task.Replicas < 1 {
		problems = append(problems, "task.replicas must be >= 1")
	}
	if req.Task.MemoryPerReplica == nil {
		problems = append(problems, "task.memory_per_replica_mb is required")
	} else if *req.Task.MemoryPerReplica < 0 {
		problems = append(problems, "task.memory_per_replica_mb must be >= 0")
	}

	devs := make([]topology.Device, 0, len(req.Cluster.Devices))
	ids := make(map[string]bool, len(req.Cluster.Devices))
	for i, d := range req.Cluster.Devices {
		if d.ID == "" {
			problems = append(problems, devicePrefix(i, "id is required"))
		} else if ids[d.ID] {
			problems = append(problems, "duplicate device id "+d.ID)
		}
		ids[d.ID] = true
		if d.MemoryMB == nil {
			problems = append(problems, devicePrefix(i, "memory_mb is required"))
			d.MemoryMB = new(int64)
		} else if *d.MemoryMB < 0 {
			problems = append(problems, devicePrefix(i, "memory_mb must be >= 0"))
		}
		if d.UsedMemory < 0 {
			problems = append(problems, devicePrefix(i, "used_memory_mb must be >= 0"))
		}
		if d.MemoryMB != nil && d.UsedMemory > *d.MemoryMB {
			problems = append(problems, devicePrefix(i, "used_memory_mb exceeds memory_mb"))
		}
		if d.NumaNode == nil {
			problems = append(problems, devicePrefix(i, "numa_node is required"))
			n := 0
			d.NumaNode = &n
		} else if *d.NumaNode < 0 {
			problems = append(problems, devicePrefix(i, "numa_node must be >= 0"))
		}
		devs = append(devs, topology.Device{
			ID:         d.ID,
			MemoryMB:   *d.MemoryMB,
			UsedMemory: d.UsedMemory,
			NumaNode:   *d.NumaNode,
		})
	}

	links := make([]topology.Link, 0, len(req.Cluster.Links))
	for i, l := range req.Cluster.Links {
		if l.A == "" || l.B == "" {
			problems = append(problems, linkPrefix(i, "both a and b are required"))
		}
		if l.A != "" && !ids[l.A] {
			problems = append(problems, linkPrefix(i, "a references unknown device "+l.A))
		}
		if l.B != "" && !ids[l.B] {
			problems = append(problems, linkPrefix(i, "b references unknown device "+l.B))
		}
		if l.A == l.B && l.A != "" {
			problems = append(problems, linkPrefix(i, "self links are not allowed"))
		}
		if l.Cost < 0 {
			problems = append(problems, linkPrefix(i, "cost must be >= 0"))
		}
		links = append(links, topology.Link{A: l.A, B: l.B, Cost: l.Cost})
	}

	var sameNuma, crossNuma int64 = 1, 10
	if req.Cluster.Defaults.SameNumaCost != nil {
		if *req.Cluster.Defaults.SameNumaCost < 0 {
			problems = append(problems, "cluster.default_link_cost.same_numa_cost must be >= 0")
		} else {
			sameNuma = *req.Cluster.Defaults.SameNumaCost
		}
	}
	if req.Cluster.Defaults.CrossNumaCost != nil {
		if *req.Cluster.Defaults.CrossNumaCost < 0 {
			problems = append(problems, "cluster.default_link_cost.cross_numa_cost must be >= 0")
		} else {
			crossNuma = *req.Cluster.Defaults.CrossNumaCost
		}
	}

	if len(problems) > 0 {
		return nil, solver.Request{}, errors.New(strings.Join(problems, "; "))
	}

	c := &topology.Cluster{
		Devices:          devs,
		Links:            links,
		DefaultSameNuma:  sameNuma,
		DefaultCrossNuma: crossNuma,
	}
	sr := solver.Request{
		Name:             req.Task.Name,
		Replicas:         *req.Task.Replicas,
		MemoryPerReplica: *req.Task.MemoryPerReplica,
		MaxSearchStates:  req.Task.MaxSearchStates,
	}
	return c, sr, nil
}

func devicePrefix(i int, msg string) string {
	return "cluster.devices[" + strconv.Itoa(i) + "]: " + msg
}

func linkPrefix(i int, msg string) string {
	return "cluster.links[" + strconv.Itoa(i) + "]: " + msg
}

func toResponse(r *solver.Result, taskName string) placementResponse {
	resp := placementResponse{
		Status:             r.Status,
		Reason:             r.Reason,
		Message:            r.Message,
		TaskName:           taskName,
		Replicas:           r.Replicas,
		MemoryPerReplicaMB: r.MemoryPerReplica,
		Placement:          make([]placementEntryResp, 0, len(r.Placement)),
		Pairs:              make([]pairResp, 0, len(r.Pairs)),
		TotalCost:          r.TotalCost,
		CrossNumaPairs:     r.CrossNumaPairs,
		Diagnostics: diagnostics{
			Strategy:       r.Strategy,
			StatesExplored: r.StatesExplored,
			Combinations:   r.Combinations,
			FreeMemory:     make([]freeMemoryResp, 0, len(r.FreeMemory)),
		},
	}
	for _, p := range r.Placement {
		resp.Placement = append(resp.Placement, placementEntryResp(p))
	}
	for _, p := range r.Pairs {
		resp.Pairs = append(resp.Pairs, pairResp(p))
	}
	for _, f := range r.FreeMemory {
		resp.Diagnostics.FreeMemory = append(resp.Diagnostics.FreeMemory, freeMemoryResp(f))
	}
	if r.Status == solver.StatusInfeasible {
		resp.Rejected = &rejectionDetail{
			RequiredReplicas: r.Replicas,
			EligibleDevices:  r.EligibleCount,
			TotalDevices:     r.TotalDevices,
			RequiredFreeMB:   r.MemoryPerReplica,
			LargestFreeMB:    r.LargestFreeMB,
			FreeMemory:       resp.Diagnostics.FreeMemory,
		}
		// Feasible-only fields are meaningless on rejection.
		resp.Placement = nil
		resp.Pairs = nil
		resp.TotalCost = 0
		resp.CrossNumaPairs = 0
	}
	return resp
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		// Headers already sent; log via the default logger.
		log.Printf("failed to encode response: %v", err)
	}
}

// loggingMiddleware records one line per request.
func loggingMiddleware(next http.Handler, logger *log.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		logger.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rw.status, time.Since(start).Round(time.Microsecond))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// NewLoggedMux returns routes wrapped with request logging.
func NewLoggedMux(logger *log.Logger) http.Handler {
	h := &Handler{Logger: logger}
	return loggingMiddleware(h.NewMux(), logger)
}
