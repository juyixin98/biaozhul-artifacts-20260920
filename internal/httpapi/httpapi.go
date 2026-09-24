// Package httpapi 提供定时触发器服务的 REST 接口（仅标准库 net/http）。
package httpapi

import (
	"encoding/json"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"time"

	"tztrigger/internal/engine"
	"tztrigger/internal/tzdb"
)

// Server 把 HTTP 请求转发到调度引擎。
type Server struct {
	eng *engine.Engine
	mux *http.ServeMux
}

// New 构造路由。Go 1.22+ 的 ServeMux 支持 "METHOD /path/{param}" 模式。
func New(eng *engine.Engine) *Server {
	s := &Server{eng: eng, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz", s.healthz)
	s.mux.HandleFunc("GET /version", s.version)
	s.mux.HandleFunc("POST /triggers", s.createTrigger)
	s.mux.HandleFunc("GET /triggers", s.listTriggers)
	s.mux.HandleFunc("GET /triggers/{id}", s.getTrigger)
	s.mux.HandleFunc("DELETE /triggers/{id}", s.deleteTrigger)
	s.mux.HandleFunc("GET /triggers/{id}/preview", s.preview)
	s.mux.HandleFunc("GET /events", s.events)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// ---- DTO ----

type createTriggerReq struct {
	ID           string `json:"id"`
	Minutes      string `json:"minutes"`
	Hours        string `json:"hours"`
	Weekdays     string `json:"weekdays"`
	Timezone     string `json:"timezone"`
	CatchUpLimit *int   `json:"catch_up_limit"`
}

type fireInfo struct {
	UTC       string `json:"utc"`
	WallClock string `json:"wall_clock"`
	LogicalID string `json:"logical_id"`
}

type triggerResp struct {
	ID            string    `json:"id"`
	Minutes       string    `json:"minutes"`
	Hours         string    `json:"hours"`
	Weekdays      string    `json:"weekdays"`
	Timezone      string    `json:"timezone"`
	CatchUpLimit  int       `json:"catch_up_limit"`
	CreatedAt     time.Time `json:"created_at"`
	TotalFired    int       `json:"total_fired"`
	MissedDropped int       `json:"missed_dropped"`
	DedupSkipped  int       `json:"dedup_skipped"`
	NextFire      *fireInfo `json:"next_fire,omitempty"`
}

// ---- handlers ----

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) version(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"go_version":      runtime.Version(),
		"tzdata_version":  tzdb.Version,
		"zoneinfo_sha256": tzdb.ZipSHA256,
	})
}

func (s *Server) createTrigger(w http.ResponseWriter, r *http.Request) {
	var req createTriggerReq
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	if req.Minutes == "" || req.Hours == "" || req.Weekdays == "" || req.Timezone == "" {
		writeErr(w, http.StatusBadRequest, "minutes/hours/weekdays/timezone 均为必填")
		return
	}
	limit := 100
	if req.CatchUpLimit != nil {
		limit = *req.CatchUpLimit
	}
	t, err := s.eng.AddTrigger(req.ID, req.Minutes, req.Hours, req.Weekdays, req.Timezone, limit)
	if err != nil {
		status := http.StatusBadRequest
		if strings.Contains(err.Error(), "已存在") {
			status = http.StatusConflict
		}
		writeErr(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, s.toResp(t))
}

func (s *Server) listTriggers(w http.ResponseWriter, r *http.Request) {
	ts := s.eng.ListTriggers()
	out := make([]triggerResp, 0, len(ts))
	for _, t := range ts {
		out = append(out, s.toResp(t))
	}
	writeJSON(w, http.StatusOK, map[string]any{"triggers": out})
}

func (s *Server) getTrigger(w http.ResponseWriter, r *http.Request) {
	t, ok := s.eng.GetTrigger(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "触发器不存在")
		return
	}
	writeJSON(w, http.StatusOK, s.toResp(t))
}

func (s *Server) deleteTrigger(w http.ResponseWriter, r *http.Request) {
	if !s.eng.DeleteTrigger(r.PathValue("id")) {
		writeErr(w, http.StatusNotFound, "触发器不存在")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) preview(w http.ResponseWriter, r *http.Request) {
	t, ok := s.eng.GetTrigger(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "触发器不存在")
		return
	}
	q := r.URL.Query()
	now := time.Now()
	from, err := parseTimeParam(q.Get("from"), now)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "from 参数需为 RFC3339: "+err.Error())
		return
	}
	to, err := parseTimeParam(q.Get("to"), from.Add(7*24*time.Hour))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "to 参数需为 RFC3339: "+err.Error())
		return
	}
	limit := 100
	if v := q.Get("limit"); v != "" {
		limit, err = strconv.Atoi(v)
		if err != nil || limit < 1 || limit > 1000 {
			writeErr(w, http.StatusBadRequest, "limit 需在 [1,1000] 内")
			return
		}
	}
	instants, truncated := s.eng.Preview(t, from, to, limit)
	fires := make([]fireInfo, 0, len(instants))
	for _, at := range instants {
		fires = append(fires, fireInfo{
			UTC:       at.UTC().Format(time.RFC3339),
			WallClock: at.In(t.Loc).Format("2006-01-02T15:04"),
			LogicalID: engine.LogicalID(t.ID, at, t.Loc),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"trigger_id": t.ID,
		"from":       from.UTC().Format(time.RFC3339),
		"to":         to.UTC().Format(time.RFC3339),
		"truncated":  truncated,
		"fires":      fires,
	})
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := 100
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			writeErr(w, http.StatusBadRequest, "limit 需为正整数")
			return
		}
		limit = n
	}
	evs := s.eng.Events(q.Get("trigger_id"), limit)
	if evs == nil {
		evs = []engine.Event{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs})
}

// ---- helpers ----

func (s *Server) toResp(t *engine.Trigger) triggerResp {
	resp := triggerResp{
		ID: t.ID, Minutes: t.Minutes, Hours: t.Hours, Weekdays: t.Weekdays,
		Timezone: t.Timezone, CatchUpLimit: t.CatchUpLimit, CreatedAt: t.CreatedAt,
		TotalFired: t.TotalFired, MissedDropped: t.MissedDropped, DedupSkipped: t.DedupSkipped,
	}
	if next, ok := s.eng.NextFire(t, time.Now()); ok {
		resp.NextFire = &fireInfo{
			UTC:       next.UTC().Format(time.RFC3339),
			WallClock: next.In(t.Loc).Format("2006-01-02T15:04"),
			LogicalID: engine.LogicalID(t.ID, next, t.Loc),
		}
	}
	return resp
}

func parseTimeParam(v string, def time.Time) (time.Time, error) {
	if v == "" {
		return def, nil
	}
	return time.Parse(time.RFC3339, v)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
