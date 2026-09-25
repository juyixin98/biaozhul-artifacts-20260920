// Package httpapi 暴露契约兼容性检查的本地 HTTP 接口，
// 并提供一个通过故障注入客户端调用进程内假服务的演练端点。
package httpapi

import (
	"encoding/json"
	"net/http"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/compat"
	"contractcheck/internal/faultclient"
	"contractcheck/internal/registry"
	"contractcheck/internal/schema"
)

// Server 持有 HTTP 处理器及其依赖。
type Server struct {
	Store *registry.Store
	Clk   clock.Clock
	Mux   *http.ServeMux
}

// NewServer 组装路由。clk 为 nil 时使用真实时钟。
func NewServer(store *registry.Store, clk clock.Clock) *Server {
	if clk == nil {
		clk = clock.Real{}
	}
	s := &Server{Store: store, Clk: clk, Mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.Mux.HandleFunc("GET /healthz", s.handleHealth)
	s.Mux.HandleFunc("POST /v1/contracts", s.handlePutContract)
	s.Mux.HandleFunc("GET /v1/contracts", s.handleListContracts)
	s.Mux.HandleFunc("POST /v1/compat/check", s.handleCheck)
	s.Mux.HandleFunc("POST /v1/probe", s.handleProbe)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type putContractReq struct {
	Name    string         `json:"name"`
	Version string         `json:"version"`
	Schema  map[string]any `json:"schema"`
}

func (s *Server) handlePutContract(w http.ResponseWriter, r *http.Request) {
	var req putContractReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Name == "" || req.Version == "" || req.Schema == nil {
		writeError(w, http.StatusBadRequest, "name、version、schema 均为必填")
		return
	}
	s.Store.Put(registry.Contract{Name: req.Name, Version: req.Version, Schema: req.Schema})
	writeJSON(w, http.StatusCreated, map[string]string{
		"name": req.Name, "version": req.Version, "status": "stored",
	})
}

func (s *Server) handleListContracts(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"contracts": s.Store.List()})
}

// sideRef 既可以引用仓库中的契约，也可以内联提供 schema。
type sideRef struct {
	Name    string         `json:"name"`
	Version string         `json:"version"`
	Schema  map[string]any `json:"schema"`
}

type checkReq struct {
	Direction compat.Direction `json:"direction"`
	Old       sideRef          `json:"old"`
	New       sideRef          `json:"new"`
}

func (s *Server) handleCheck(w http.ResponseWriter, r *http.Request) {
	var req checkReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Direction != compat.DirectionRequest && req.Direction != compat.DirectionResponse {
		writeError(w, http.StatusBadRequest, "direction 必须是 request 或 response")
		return
	}
	oldSchema, err := s.resolve(req.Old)
	if err != nil {
		writeError(w, http.StatusBadRequest, "old: "+err.Error())
		return
	}
	newSchema, err := s.resolve(req.New)
	if err != nil {
		writeError(w, http.StatusBadRequest, "new: "+err.Error())
		return
	}
	report := compat.Check(req.Direction, oldSchema, newSchema, s.Clk.Now())
	status := http.StatusOK
	writeJSON(w, status, report)
}

func (s *Server) resolve(ref sideRef) (*schema.Schema, error) {
	raw, err := json.Marshal(ref.Schema)
	if err != nil {
		return nil, err
	}
	if ref.Schema != nil {
		return schema.ParseBytes(raw)
	}
	if ref.Name == "" || ref.Version == "" {
		return nil, errBadRef
	}
	c, err := s.Store.Get(ref.Name, ref.Version)
	if err != nil {
		return nil, err
	}
	raw, err = json.Marshal(c.Schema)
	if err != nil {
		return nil, err
	}
	return schema.ParseBytes(raw)
}

type probeReq struct {
	Fault struct {
		Kind       string `json:"kind"`
		StatusCode int    `json:"statusCode"`
		FlakyTimes int    `json:"flakyTimes"`
		DelayMS    int64  `json:"delayMs"`
	} `json:"fault"`
	Client struct {
		MaxAttempts      int   `json:"maxAttempts"`
		InitialBackoffMS int64 `json:"initialBackoffMs"`
		MaxBackoffMS     int64 `json:"maxBackoffMs"`
		// UseFakeTime 为 true 时退避不占用真实时间，并用可控时钟记录耗时。
		UseFakeTime bool `json:"useFakeTime"`
	} `json:"client"`
}

func (s *Server) handleProbe(w http.ResponseWriter, r *http.Request) {
	var req probeReq
	if err := decode(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	spec := faultclient.FaultSpec{
		Kind:       faultclient.FaultKind(req.Fault.Kind),
		StatusCode: req.Fault.StatusCode,
		FlakyTimes: req.Fault.FlakyTimes,
		Delay:      time.Duration(req.Fault.DelayMS) * time.Millisecond,
	}
	if spec.Kind == "" {
		spec.Kind = faultclient.FaultNone
	}
	server := faultclient.NewFakeServer(spec)
	defer server.Close()

	cfg := faultclient.Config{
		MaxAttempts:    req.Client.MaxAttempts,
		InitialBackoff: time.Duration(req.Client.InitialBackoffMS) * time.Millisecond,
		MaxBackoff:     time.Duration(req.Client.MaxBackoffMS) * time.Millisecond,
	}

	clk := s.Clk
	var sleeper faultclient.Sleeper = faultclient.RealSleeper{}
	if req.Client.UseFakeTime {
		fake := clock.NewFake(s.Clk.Now())
		clk = fake
		sleeper = faultclient.NewFakeSleeper(fake)
		// 假时钟模式下给一个不会真正等待、也不会被短超时打断的 HTTP 客户端。
		cfg.HTTPClient = &http.Client{Timeout: 5 * time.Second}
	}
	c := faultclient.New(cfg, clk, sleeper)
	result := c.Get(server.URL)
	writeJSON(w, http.StatusOK, result)
}

func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

var errBadRef = &simpleError{"必须提供内联 schema，或提供仓库引用 name+version"}

type simpleError struct{ s string }

func (e *simpleError) Error() string { return e.s }
