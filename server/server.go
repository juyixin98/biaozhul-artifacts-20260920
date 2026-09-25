// Package server exposes the sched library over a small local HTTP API.
//
// Endpoints:
//
//	POST   /v1/resources                 add a resource
//	GET    /v1/resources                 list resources
//	GET    /v1/resources/{id}            get one resource
//	POST   /v1/earliest                  earliest feasible slot query
//	POST   /v1/reservations:batch        atomic batch reservation
//	GET    /v1/batches/{id}              inspect a batch
//	GET    /v1/reservations              list reservations (?resource_id=)
//	GET    /v1/reservations/{id}         get one reservation
//	POST   /v1/pump                      run lifecycle transitions now
//	GET    /v1/events                    structured state-change events
//	GET    /v1/snapshot                  whole-store snapshot
//	GET    /healthz                      liveness
//
// All times are RFC3339; durations are Go duration strings ("90m", "2h30m").
package server

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"rsrv/sched"
)

// Duration is time.Duration with JSON (un)marshaling as a duration string.
type Duration time.Duration

// MarshalJSON encodes the duration as a quoted Go-duration string.
func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(time.Duration(d).String())
}

// UnmarshalJSON accepts a quoted Go-duration string ("90m").
func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	*d = Duration(v)
	return nil
}

// Std converts to time.Duration.
func (d Duration) Std() time.Duration { return time.Duration(d) }

// API request/response shapes.
type resourceReq struct {
	ID       string           `json:"id"`
	Capacity map[string]int64 `json:"capacity"`
}

type intervalDTO struct {
	Start time.Time `json:"start"`
	End   time.Time `json:"end"`
}

type earliestReq struct {
	ResourceID  string           `json:"resource_id"`
	WindowStart time.Time        `json:"window_start"`
	WindowEnd   *time.Time       `json:"window_end"`
	Duration    Duration         `json:"duration"`
	Demand      map[string]int64 `json:"demand"`
}

type batchItemReq struct {
	ID          string           `json:"id"`
	ResourceID  string           `json:"resource_id"`
	Kind        sched.ItemKind   `json:"kind"`
	Demand      map[string]int64 `json:"demand"`
	Duration    *Duration        `json:"duration,omitempty"`
	Interval    *intervalDTO     `json:"interval,omitempty"`
	WindowStart *time.Time       `json:"window_start,omitempty"`
	WindowEnd   *time.Time       `json:"window_end,omitempty"`
}

type batchReq struct {
	ID        string         `json:"id"`
	RequestID string         `json:"request_id,omitempty"`
	Items     []batchItemReq `json:"items"`
}

type plannedItemDTO struct {
	ItemID        string         `json:"item_id"`
	ResourceID    string         `json:"resource_id"`
	Kind          sched.ItemKind `json:"kind"`
	Start         time.Time      `json:"start"`
	End           time.Time      `json:"end"`
	ReservationID string         `json:"reservation_id"`
}

type batchResp struct {
	ID          string           `json:"id"`
	RequestID   string           `json:"request_id,omitempty"`
	CommittedAt time.Time        `json:"committed_at"`
	Planned     []plannedItemDTO `json:"planned"`
}

// Server wires a scheduler to HTTP handlers.
type Server struct {
	sch *sched.Scheduler
	mux *http.ServeMux
}

// New builds the HTTP server around sch.
func New(sch *sched.Scheduler) *Server {
	s := &Server{sch: sch, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler returns the root http.Handler.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	m := s.mux
	m.HandleFunc("POST /v1/resources", s.handleAddResource)
	m.HandleFunc("GET /v1/resources", s.handleListResources)
	m.HandleFunc("GET /v1/resources/{id}", s.handleGetResource)
	m.HandleFunc("POST /v1/earliest", s.handleEarliest)
	m.HandleFunc("POST /v1/reservations:batch", s.handleBatch)
	m.HandleFunc("GET /v1/batches/{id}", s.handleGetBatch)
	m.HandleFunc("GET /v1/reservations", s.handleListReservations)
	m.HandleFunc("GET /v1/reservations/{id}", s.handleGetReservation)
	m.HandleFunc("POST /v1/pump", s.handlePump)
	m.HandleFunc("GET /v1/events", s.handleEvents)
	m.HandleFunc("GET /v1/snapshot", s.handleSnapshot)
	m.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func errorBody(code, reason, msg string) map[string]any {
	return map[string]any{"error": map[string]string{"code": code, "reason": reason, "message": msg}}
}

func writeError(w http.ResponseWriter, status int, reason, msg string) {
	writeJSON(w, status, errorBody(reason, reason, msg))
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

// mapSchedulerError translates library errors to HTTP status codes.
func mapSchedulerError(w http.ResponseWriter, err error) {
	if ve, ok := sched.AsValidationError(err); ok {
		status := http.StatusBadRequest
		switch ve.Reason {
		case sched.ReasonResourceNotFound, sched.ReasonItemNotFound:
			status = http.StatusNotFound
		case sched.ReasonDuplicateResID, sched.ReasonDuplicateItem:
			status = http.StatusConflict
		}
		writeJSON(w, status, errorBody(ve.Reason, ve.Reason, ve.Message))
		return
	}
	if bce, ok := sched.AsBatchConflictError(err); ok {
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": errorBody("batch_conflict", "batch_conflict", bce.Error()),
			"batch": bce,
		})
		return
	}
	writeError(w, http.StatusInternalServerError, "internal", err.Error())
}

func (s *Server) handleAddResource(w http.ResponseWriter, r *http.Request) {
	var req resourceReq
	if !decode(w, r, &req) {
		return
	}
	res := &sched.Resource{ID: req.ID, Capacity: sched.Dims(req.Capacity)}
	if err := s.sch.AddResource(res); err != nil {
		mapSchedulerError(w, err)
		return
	}
	got, _ := s.sch.GetResource(req.ID)
	writeJSON(w, http.StatusCreated, got)
}

func (s *Server) handleListResources(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"resources": s.sch.Resources()})
}

func (s *Server) handleGetResource(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	res, ok := s.sch.GetResource(id)
	if !ok {
		writeError(w, http.StatusNotFound, sched.ReasonResourceNotFound, "resource not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) handleEarliest(w http.ResponseWriter, r *http.Request) {
	var req earliestReq
	if !decode(w, r, &req) {
		return
	}
	var winEnd time.Time
	if req.WindowEnd != nil {
		winEnd = *req.WindowEnd
	}
	t, ok, err := s.sch.EarliestFeasible(req.ResourceID, req.WindowStart, winEnd, req.Duration.Std(), sched.Dims(req.Demand))
	if err != nil {
		mapSchedulerError(w, err)
		return
	}
	resp := map[string]any{"feasible": ok, "resource_id": req.ResourceID}
	if ok {
		resp["start"] = t
		resp["end"] = t.Add(req.Duration.Std())
	} else {
		resp["reason"] = sched.ReasonNoFeasibleSlot
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleBatch(w http.ResponseWriter, r *http.Request) {
	var req batchReq
	if !decode(w, r, &req) {
		return
	}
	br := &sched.BatchRequest{ID: req.ID, RequestID: req.RequestID}
	for _, it := range req.Items {
		bi := sched.BatchItem{
			ID: it.ID, ResourceID: it.ResourceID, Kind: it.Kind,
			Demand: sched.Dims(it.Demand),
		}
		switch it.Kind {
		case sched.ItemFixed:
			if it.Interval == nil {
				writeError(w, http.StatusBadRequest, "missing_interval", "fixed item requires an interval")
				return
			}
			bi.Interval = sched.Interval{Start: it.Interval.Start, End: it.Interval.End}
		case sched.ItemEarliest:
			if it.Duration == nil {
				writeError(w, http.StatusBadRequest, "missing_duration", "earliest item requires a duration")
				return
			}
			bi.Duration = it.Duration.Std()
			if it.WindowStart != nil {
				bi.WindowStart = *it.WindowStart
			}
			if it.WindowEnd != nil {
				bi.WindowEnd = *it.WindowEnd
			}
		default:
			writeError(w, http.StatusBadRequest, "bad_kind", "item kind must be fixed or earliest")
			return
		}
		br.Items = append(br.Items, bi)
	}
	b, err := s.sch.CommitBatch(br)
	if err != nil {
		mapSchedulerError(w, err)
		return
	}
	resp := batchResp{ID: b.ID, RequestID: b.RequestID, CommittedAt: b.CommittedAt}
	for i, pl := range b.Planned {
		resp.Planned = append(resp.Planned, plannedItemDTO{
			ItemID: pl.Item.ID, ResourceID: pl.Item.ResourceID, Kind: pl.Item.Kind,
			Start: pl.Start, End: pl.End, ReservationID: b.ReservationIDs[i],
		})
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (s *Server) handleGetBatch(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	b, ok := s.sch.GetBatch(id)
	if !ok {
		writeError(w, http.StatusNotFound, "batch_not_found", "batch not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, b)
}

func (s *Server) handleListReservations(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"reservations": s.sch.Reservations(r.URL.Query().Get("resource_id"))})
}

func (s *Server) handleGetReservation(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	rr, ok := s.sch.GetReservation(id)
	if !ok {
		writeError(w, http.StatusNotFound, "reservation_not_found", "reservation not found: "+id)
		return
	}
	writeJSON(w, http.StatusOK, rr)
}

func (s *Server) handlePump(w http.ResponseWriter, _ *http.Request) {
	s.sch.Pump()
	writeJSON(w, http.StatusOK, map[string]any{"status": "pumped", "at": time.Now().UTC()})
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	log := s.sch.EventLog()
	if log == nil {
		writeJSON(w, http.StatusOK, map[string]any{"events": []sched.Event{}})
		return
	}
	var after int64 = -1
	if v := r.URL.Query().Get("after_seq"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bad_request", "after_seq must be an integer")
			return
		}
		after = n
	}
	var evs []sched.Event
	if after < 0 {
		evs = log.Events()
	} else {
		evs = log.Since(after)
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs})
}

func (s *Server) handleSnapshot(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.sch.Snapshot())
}
