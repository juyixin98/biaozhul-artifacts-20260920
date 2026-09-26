// Package registry is an in-process fake contract registry. It stands in
// for any external contract store so the service never talks to production
// systems.
package registry

import (
	"encoding/json"
	"net/http"
	"sync"
)

// Contract is one versioned service contract.
type Contract struct {
	Service  string          `json:"service"`
	Version  string          `json:"version"`
	Request  json.RawMessage `json:"request"`
	Response json.RawMessage `json:"response"`
}

// Store holds contracts in memory and serves them over HTTP.
type Store struct {
	mu        sync.RWMutex
	contracts map[string]map[string]Contract // service -> version -> contract
}

func NewStore() *Store {
	return &Store{contracts: map[string]map[string]Contract{}}
}

// Put inserts or replaces a contract.
func (s *Store) Put(c Contract) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.contracts[c.Service] == nil {
		s.contracts[c.Service] = map[string]Contract{}
	}
	s.contracts[c.Service][c.Version] = c
}

// Get returns a contract and whether it exists.
func (s *Store) Get(service, version string) (Contract, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.contracts[service][version]
	return c, ok
}

// Handler exposes the store as an HTTP API:
//
//	GET /contracts/{service}/{version}
func (s *Store) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /contracts/{service}/{version}", func(w http.ResponseWriter, r *http.Request) {
		c, ok := s.Get(r.PathValue("service"), r.PathValue("version"))
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "contract not found"})
			return
		}
		writeJSON(w, http.StatusOK, c)
	})
	return mux
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
