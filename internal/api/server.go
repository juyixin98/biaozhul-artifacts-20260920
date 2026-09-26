// Package api exposes the contract compatibility checker over HTTP.
package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/compat"
	"contractcheck/internal/registry"
	"contractcheck/internal/schema"
)

// Server is the compatibility-check HTTP service.
type Server struct {
	// RegistryBaseURL points at the (fake, in-process) contract registry.
	RegistryBaseURL string
	// Client is used for registry calls; it may wrap a fault-injecting
	// transport.
	Client *http.Client
	Clock  clock.Clock

	mux *http.ServeMux
}

// NewServer builds a Server with its routes registered.
func NewServer(registryBaseURL string, client *http.Client, clk clock.Clock) *Server {
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	if clk == nil {
		clk = clock.Real{}
	}
	s := &Server{RegistryBaseURL: registryBaseURL, Client: client, Clock: clk, mux: http.NewServeMux()}
	s.mux.HandleFunc("GET /healthz", s.handleHealth)
	s.mux.HandleFunc("POST /v1/compatibility/check", s.handleCheck)
	s.mux.HandleFunc("POST /v1/compatibility/checkInline", s.handleCheckInline)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.mux.ServeHTTP(w, r) }

// Report is the structured result of comparing two contract versions.
type Report struct {
	Service     string        `json:"service,omitempty"`
	FromVersion string        `json:"fromVersion,omitempty"`
	ToVersion   string        `json:"toVersion,omitempty"`
	CheckedAt   time.Time     `json:"checkedAt"`
	Status      string        `json:"status"`
	Request     compat.Result `json:"request"`
	Response    compat.Result `json:"response"`
}

type checkRequest struct {
	Service     string `json:"service"`
	FromVersion string `json:"fromVersion"`
	ToVersion   string `json:"toVersion"`
}

type contractPair struct {
	Request  json.RawMessage `json:"request"`
	Response json.RawMessage `json:"response"`
}

type inlineRequest struct {
	Old contractPair `json:"old"`
	New contractPair `json:"new"`
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleCheck(w http.ResponseWriter, r *http.Request) {
	var req checkRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Service == "" || req.FromVersion == "" || req.ToVersion == "" {
		writeError(w, http.StatusBadRequest, "service, fromVersion and toVersion are required")
		return
	}
	oldContract, err := s.fetchContract(req.Service, req.FromVersion, r.Header)
	if err != nil {
		writeError(w, http.StatusBadGateway, fmt.Sprintf("fetching old contract: %v", err))
		return
	}
	newContract, err := s.fetchContract(req.Service, req.ToVersion, r.Header)
	if err != nil {
		writeError(w, http.StatusBadGateway, fmt.Sprintf("fetching new contract: %v", err))
		return
	}
	report, err := s.compare(contractPair{Request: oldContract.Request, Response: oldContract.Response},
		contractPair{Request: newContract.Request, Response: newContract.Response})
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	report.Service = req.Service
	report.FromVersion = req.FromVersion
	report.ToVersion = req.ToVersion
	writeJSON(w, http.StatusOK, report)
}

func (s *Server) handleCheckInline(w http.ResponseWriter, r *http.Request) {
	var req inlineRequest
	if err := decodeBody(r, &req); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(req.Old.Request) == 0 || len(req.Old.Response) == 0 ||
		len(req.New.Request) == 0 || len(req.New.Response) == 0 {
		writeError(w, http.StatusBadRequest, "old.request, old.response, new.request and new.response are required")
		return
	}
	report, err := s.compare(req.Old, req.New)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, report)
}

// compare runs both directional checks and combines them into a Report.
func (s *Server) compare(old, new contractPair) (*Report, error) {
	oldReq, err := schema.Parse(old.Request)
	if err != nil {
		return nil, fmt.Errorf("old request schema: %w", err)
	}
	newReq, err := schema.Parse(new.Request)
	if err != nil {
		return nil, fmt.Errorf("new request schema: %w", err)
	}
	oldResp, err := schema.Parse(old.Response)
	if err != nil {
		return nil, fmt.Errorf("old response schema: %w", err)
	}
	newResp, err := schema.Parse(new.Response)
	if err != nil {
		return nil, fmt.Errorf("new response schema: %w", err)
	}
	report := &Report{
		CheckedAt: s.Clock.Now().UTC(),
		Request:   compat.Check(oldReq, newReq, compat.Request),
		Response:  compat.Check(oldResp, newResp, compat.Response),
	}
	report.Status = worstStatus(report.Request.Status, report.Response.Status)
	return report, nil
}

// faultHeaders are forwarded from the inbound request to registry calls so
// callers can drive fault injection per request.
var faultHeaders = []string{"X-Fault-Latency-Ms", "X-Fault-Error-Rate"}

func (s *Server) fetchContract(service, version string, inbound http.Header) (*registry.Contract, error) {
	url := fmt.Sprintf("%s/contracts/%s/%s", s.RegistryBaseURL, service, version)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	for _, k := range faultHeaders {
		if v := inbound.Get(k); v != "" {
			req.Header.Set(k, v)
		}
	}
	resp, err := s.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("contract %s@%s not found", service, version)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("registry returned status %d", resp.StatusCode)
	}
	var c registry.Contract
	if err := json.NewDecoder(resp.Body).Decode(&c); err != nil {
		return nil, fmt.Errorf("decoding registry response: %w", err)
	}
	return &c, nil
}

// worstStatus ranks incompatible > unknown > compatible.
func worstStatus(statuses ...string) string {
	rank := map[string]int{
		compat.StatusCompatible:   0,
		compat.StatusUnknown:      1,
		compat.StatusIncompatible: 2,
	}
	worst := compat.StatusCompatible
	for _, st := range statuses {
		if rank[st] > rank[worst] {
			worst = st
		}
	}
	return worst
}

func decodeBody(r *http.Request, v any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("reading body: %w", err)
	}
	if err := json.Unmarshal(body, v); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
