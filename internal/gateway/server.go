// Package gateway is an in-process FAKE external payment service. It is a
// real HTTP server on its own port, so network timeouts, connection resets
// and duplicate deliveries behave like a real dependency, but nothing here
// leaves the machine or touches a production system.
//
// The fake models the exact boundary this project is about:
//
//   - A charge carrying Idempotency-Key is deduplicated by the gateway, so
//     an ambiguous timeout followed by a retry results in ONE charge.
//   - A charge WITHOUT that key is treated as a new payment every time, so
//     retrying an ambiguous timeout results in MULTIPLE charges. The local
//     idempotency transaction cannot prevent that — exactly-once for an
//     arbitrary external call is NOT guaranteed.
package gateway

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Faults the fake can be told to perform on a single call. They are honored
// once per (fault, key) pair so a retried charge can still succeed.
const (
	// FaultResetBeforeCharge closes the TCP connection WITHOUT charging.
	FaultResetBeforeCharge = "reset-before-charge"
	// FaultResetAfterCharge charges first, then closes the connection
	// without responding: the classic ambiguous outcome.
	FaultResetAfterCharge = "reset-after-charge"
	// FaultHang charges first, then holds the connection until the client
	// gives up (or d, if positive).
	FaultHang = "hang"
	// FaultError500 answers 500 without charging.
	FaultError500 = "error500"
)

// Charge is a payment stored by the fake gateway.
type Charge struct {
	ID        string    `json:"id"`
	Key       string    `json:"idempotency_key,omitempty"`
	Amount    int       `json:"amount"`
	Currency  string    `json:"currency"`
	Reference string    `json:"reference"`
	CreatedAt time.Time `json:"created_at"`
}

// Metrics reports gateway activity, used by assertions and structured
// test results.
type Metrics struct {
	HTTPAttempts   int      `json:"http_attempts"`
	Charges        int      `json:"charges_created"`
	ChargeIDs      []string `json:"charge_ids"`
	InjectedFaults []string `json:"injected_faults"`
}

type chargeReq struct {
	IdempotencyKey string `json:"idempotency_key,omitempty"`
	Amount         int    `json:"amount"`
	Currency       string `json:"currency"`
	Reference      string `json:"reference,omitempty"`
}

// Server is the fake payment gateway.
type Server struct {
	mu sync.Mutex

	byKey   map[string]Charge // deduplication index (only keyed charges)
	charges []Charge
	attempt int
	faults  []string

	// consumedFaults ensures each (fault,key) pair fires at most once.
	consumedFaults map[string]bool
	now            func() time.Time
}

// NewServer builds a fake gateway with the given clock (nil = real time).
func NewServer(now func() time.Time) *Server {
	if now == nil {
		now = func() time.Time { return time.Now().UTC() }
	}
	return &Server{
		byKey:          make(map[string]Charge),
		consumedFaults: make(map[string]bool),
		now:            now,
	}
}

// Handler returns the gateway's HTTP handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/gateway/charge", s.handleCharge)
	mux.HandleFunc("/gateway/metrics", s.handleMetrics)
	mux.HandleFunc("/gateway/reset", s.handleReset)
	return mux
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.mu.Lock()
	s.byKey = make(map[string]Charge)
	s.charges = nil
	s.attempt = 0
	s.faults = nil
	s.consumedFaults = make(map[string]bool)
	s.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	m := Metrics{
		HTTPAttempts:   s.attempt,
		Charges:        len(s.charges),
		ChargeIDs:      make([]string, 0, len(s.charges)),
		InjectedFaults: append([]string(nil), s.faults...),
	}
	for _, c := range s.charges {
		m.ChargeIDs = append(m.ChargeIDs, c.ID)
	}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, m)
}

func (s *Server) handleCharge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	fault := r.Header.Get("X-Fault")

	var req chargeReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"bad_json"}`, http.StatusBadRequest)
		return
	}

	s.mu.Lock()
	s.attempt++
	// A keyed charge that already exists is always deduplicated, even if
	// the caller asks for a fault: the money already moved.
	if req.IdempotencyKey != "" {
		if existing, ok := s.byKey[req.IdempotencyKey]; ok {
			s.mu.Unlock()
			writeJSON(w, http.StatusOK, map[string]any{
				"charge":  existing,
				"deduped": true,
			})
			return
		}
	}

	shouldFire := fault != "" && !s.consumedFaults[fault+"|"+req.IdempotencyKey]
	chargesBeforeFault := fault == FaultResetAfterCharge || fault == FaultHang
	if shouldFire {
		// Every fault fires at most once per (fault,key) pair, so a retry
		// after the injected failure can succeed.
		s.consumedFaults[fault+"|"+req.IdempotencyKey] = true
		s.faults = append(s.faults, fault)
		if chargesBeforeFault {
			s.recordChargeLocked(&req)
		}
	}
	hj, _ := w.(http.Hijacker)
	s.mu.Unlock()

	switch {
	case shouldFire && (fault == FaultResetAfterCharge || fault == FaultResetBeforeCharge):
		if hj != nil {
			conn, _, _ := hj.Hijack()
			if conn != nil {
				_ = conn.Close() // client sees a connection reset/EOF
				return
			}
		}
		http.Error(w, `{"error":"fault"}`, http.StatusBadGateway)
		return
	case shouldFire && fault == FaultHang:
		// Charge already recorded; hold the connection open longer than
		// any reasonable client timeout, then drop it.
		if hj != nil {
			conn, _, _ := hj.Hijack()
			if conn != nil {
				go func() {
					time.Sleep(30 * time.Second)
					_ = conn.Close()
				}()
				return
			}
		}
		time.Sleep(30 * time.Second)
		return
	case shouldFire && fault == FaultError500:
		http.Error(w, `{"error":"injected_gateway_failure"}`, http.StatusInternalServerError)
		return
	}

	// Normal path (or a fault that already fired for this key).
	s.mu.Lock()
	if req.IdempotencyKey != "" {
		if existing, ok := s.byKey[req.IdempotencyKey]; ok {
			s.mu.Unlock()
			writeJSON(w, http.StatusOK, map[string]any{
				"charge":  existing,
				"deduped": true,
			})
			return
		}
	}
	c := s.recordChargeLocked(&req)
	s.mu.Unlock()
	writeJSON(w, http.StatusCreated, map[string]any{
		"charge":  c,
		"deduped": false,
	})
}

// recordChargeLocked appends a charge; caller holds mu.
func (s *Server) recordChargeLocked(req *chargeReq) Charge {
	c := Charge{
		ID:        fmt.Sprintf("ch_%06d", len(s.charges)+1),
		Key:       req.IdempotencyKey,
		Amount:    req.Amount,
		Currency:  req.Currency,
		Reference: req.Reference,
		CreatedAt: s.now(),
	}
	s.charges = append(s.charges, c)
	if req.IdempotencyKey != "" {
		s.byKey[req.IdempotencyKey] = c
	}
	return c
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
