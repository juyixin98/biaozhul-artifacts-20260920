// Package server 提供调度器的纯 net/http JSON 接口。
package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/big"
	"net/http"

	"github.com/example/drf-scheduler/internal/scheduler"
)

// Server 持有调度器并实现 http.Handler。
type Server struct {
	sched *scheduler.Scheduler
	mux   *http.ServeMux
}

// New 创建 HTTP 服务。
func New(s *scheduler.Scheduler) *Server {
	srv := &Server{sched: s, mux: http.NewServeMux()}
	srv.routes()
	return srv
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// ---- 请求 DTO -----------------------------------------------------------

// weightNumber 接收权重，支持 JSON 数字（1、1.5、0.1）或字符串分数
// （"1/3"、"2/1"、"0.1"），最终用 big.Rat 精确解析，
// 避免 0.1 这类十进制小数经 float64 引入误差。
type weightNumber struct {
	raw string
}

func (w *weightNumber) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		w.raw = s
		return nil
	}
	var n json.Number
	if err := json.Unmarshal(b, &n); err != nil {
		return errors.New("weight must be a number or a string such as \"1/3\"")
	}
	w.raw = n.String()
	return nil
}

func (w weightNumber) rat() (*big.Rat, error) {
	if w.raw == "" {
		return big.NewRat(1, 1), nil
	}
	r, ok := new(big.Rat).SetString(w.raw)
	if !ok {
		return nil, fmt.Errorf("weight %q is not a valid rational number (use a number or a fraction like \"1/3\")", w.raw)
	}
	return r, nil
}

type createTenantReq struct {
	ID     string       `json:"id"`
	Weight weightNumber `json:"weight"`
	Quota  *resReq      `json:"quota"`
}

type resReq struct {
	CPU int64 `json:"cpu"`
	Mem int64 `json:"mem"`
}

type submitTaskReq struct {
	ID     string `json:"id"`
	Tenant string `json:"tenant"`
	CPU    int64  `json:"cpu"`
	Mem    int64  `json:"mem"`
}

type respTask struct {
	Status string             `json:"status"`
	Task   scheduler.TaskView `json:"task"`
}

// ---- 路由 ---------------------------------------------------------------

func (s *Server) routes() {
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("GET /state", s.handleState)
	s.mux.HandleFunc("POST /admin/reset", s.handleReset)

	s.mux.HandleFunc("POST /tenants", s.handleCreateTenant)
	s.mux.HandleFunc("GET /tenants/{id}", s.handleGetTenant)
	s.mux.HandleFunc("DELETE /tenants/{id}", s.handleDeleteTenant)

	s.mux.HandleFunc("POST /tasks", s.handleSubmitTask)
	s.mux.HandleFunc("GET /tasks/{id}", s.handleGetTask)
	s.mux.HandleFunc("DELETE /tasks/{id}", s.handleReleaseTask)
}

// ---- 处理函数 ------------------------------------------------------------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.sched.Snapshot())
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	s.sched.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

func (s *Server) handleCreateTenant(w http.ResponseWriter, r *http.Request) {
	var req createTenantReq
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	weight, err := req.Weight.rat()
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	spec := scheduler.TenantSpec{ID: req.ID, Weight: weight}
	if req.Quota != nil {
		spec.Quota = &scheduler.Quota{CPU: req.Quota.CPU, Mem: req.Quota.Mem}
	}
	if err := s.sched.CreateTenant(spec); err != nil {
		writeSchedError(w, err)
		return
	}
	// 创建成功后返回该租户视图。
	st := s.sched.Snapshot()
	for i := range st.Tenants {
		if st.Tenants[i].ID == req.ID {
			writeJSON(w, http.StatusCreated, st.Tenants[i])
			return
		}
	}
	writeJSON(w, http.StatusCreated, map[string]string{"id": req.ID, "status": "created"})
}

func (s *Server) handleGetTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	st := s.sched.Snapshot()
	for i := range st.Tenants {
		if st.Tenants[i].ID == id {
			writeJSON(w, http.StatusOK, st.Tenants[i])
			return
		}
	}
	writeError(w, http.StatusNotFound, "tenant %q not found", id)
}

func (s *Server) handleDeleteTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := s.sched.DeleteTenant(id); err != nil {
		writeSchedError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"id": id, "status": "deleted"})
}

func (s *Server) handleSubmitTask(w http.ResponseWriter, r *http.Request) {
	var req submitTaskReq
	if err := decodeBody(w, r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	spec := scheduler.TaskSpec{
		ID:     req.ID,
		Tenant: req.Tenant,
		Demand: scheduler.Resources{CPU: req.CPU, Mem: req.Mem},
	}
	status, err := s.sched.SubmitTask(spec)
	if err != nil {
		writeSchedError(w, err)
		return
	}
	view, _ := s.sched.TaskSnapshot(req.ID)
	writeJSON(w, http.StatusCreated, respTask{Status: status, Task: view})
}

func (s *Server) handleGetTask(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	view, ok := s.sched.TaskSnapshot(id)
	if !ok {
		writeError(w, http.StatusNotFound, "task %q not found", id)
		return
	}
	writeJSON(w, http.StatusOK, respTask{Status: view.Status, Task: view})
}

func (s *Server) handleReleaseTask(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := s.sched.ReleaseTask(id); err != nil {
		writeSchedError(w, err)
		return
	}
	// 释放可能让排队任务被放置，附带最新全局状态，方便观察重调度结果。
	writeJSON(w, http.StatusOK, map[string]any{
		"id":     id,
		"status": "released",
		"state":  s.sched.Snapshot(),
	})
}

// ---- 辅助函数 ------------------------------------------------------------

func decodeBody(w http.ResponseWriter, r *http.Request, v any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	// 拒绝尾随数据（第二个 JSON 值或垃圾字节）：正常情况应读到 EOF。
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return errors.New("unexpected trailing data in request body")
		}
		return err
	}
	return nil
}

func writeSchedError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, scheduler.ErrNotFound):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, scheduler.ErrDuplicateID):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, scheduler.ErrTenantHasTasks):
		writeError(w, http.StatusConflict, err.Error())
	case errors.Is(err, scheduler.ErrInvalidInput):
		writeError(w, http.StatusBadRequest, err.Error())
	default:
		log.Printf("unexpected scheduler error: %v", err)
		writeError(w, http.StatusInternalServerError, "internal error")
	}
}

func writeError(w http.ResponseWriter, code int, format string, args ...any) {
	writeJSON(w, code, map[string]string{"error": fmt.Sprintf(format, args...)})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("write response: %v", err)
	}
}
