// Package httpapi exposes the UDP reliable-transfer simulator over HTTP.
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"udpreliable/internal/sim"
)

type transferRequest struct {
	SizeBytes    int     `json:"sizeBytes"`
	Seed         int64   `json:"seed"`
	DropRate     float64 `json:"dropRate"`
	DupRate      float64 `json:"dupRate"`
	ReorderRate  float64 `json:"reorderRate"`
	StalePackets int     `json:"stalePackets"`
	Window       int     `json:"window"`
	ChunkSize    int     `json:"chunkSize"`
	RTOMs        int     `json:"rtoMs"`
	TimeoutMs    int     `json:"timeoutMs"`
	Transport    string  `json:"transport"` // "mem" (default) or "udp"
	InitialSeq   uint32  `json:"initialSeq"`
}

type faultStatsJSON struct {
	Sent       int `json:"sent"`
	Dropped    int `json:"dropped"`
	Duplicated int `json:"duplicated"`
	Reordered  int `json:"reordered"`
}

type transferResponse struct {
	OK         bool   `json:"ok"`
	Error      string `json:"error,omitempty"`
	Bytes      int    `json:"bytes"`
	SHA256     string `json:"sha256"`
	HashMatch  bool   `json:"hashMatch"`
	DurationMs int64  `json:"durationMs"`
	Sender     struct {
		DataSent    int `json:"dataSent"`
		Retransmits int `json:"retransmits"`
		FinSent     int `json:"finSent"`
		AcksRecv    int `json:"acksRecv"`
		StaleDrop   int `json:"staleDrop"`
		MaxInFlight int `json:"maxInFlight"`
	} `json:"sender"`
	Receiver struct {
		DataRecv  int `json:"dataRecv"`
		DupRecv   int `json:"dupRecv"`
		AcksSent  int `json:"acksSent"`
		FinRecv   int `json:"finRecv"`
		DoneSent  int `json:"doneSent"`
		StaleDrop int `json:"staleDrop"`
		MaxBuf    int `json:"maxBuf"`
	} `json:"receiver"`
	DataFaults faultStatsJSON `json:"dataFaults"`
	AckFaults  faultStatsJSON `json:"ackFaults"`
}

// NewHandler returns the simulator's HTTP handler.
func NewHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/transfers", handleTransfer)
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte(helpText))
	})
	return mux
}

func handleTransfer(w http.ResponseWriter, r *http.Request) {
	var req transferRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	cfg := sim.RunConfig{
		SizeBytes:    req.SizeBytes,
		Seed:         req.Seed,
		DropRate:     req.DropRate,
		DupRate:      req.DupRate,
		ReorderRate:  req.ReorderRate,
		StalePackets: req.StalePackets,
		Window:       req.Window,
		Chunk:        req.ChunkSize,
		RTO:          time.Duration(req.RTOMs) * time.Millisecond,
		Timeout:      time.Duration(req.TimeoutMs) * time.Millisecond,
		Transport:    req.Transport,
		InitialSeq:   req.InitialSeq,
	}
	res, err := sim.Run(cfg)
	resp := transferResponse{
		OK:         res.OK,
		Bytes:      res.Bytes,
		SHA256:     res.Hash,
		HashMatch:  res.HashMatch,
		DurationMs: res.Duration.Milliseconds(),
		DataFaults: faultStatsJSON{res.DataFaults.Sent, res.DataFaults.Dropped, res.DataFaults.Duplicated, res.DataFaults.Reordered},
		AckFaults:  faultStatsJSON{res.AckFaults.Sent, res.AckFaults.Dropped, res.AckFaults.Duplicated, res.AckFaults.Reordered},
	}
	resp.Sender.DataSent = res.Sender.DataSent
	resp.Sender.Retransmits = res.Sender.Retransmits
	resp.Sender.FinSent = res.Sender.FinSent
	resp.Sender.AcksRecv = res.Sender.AcksRecv
	resp.Sender.StaleDrop = res.Sender.StaleDrop
	resp.Sender.MaxInFlight = res.Sender.MaxInFlight
	resp.Receiver.DataRecv = res.Receiver.DataRecv
	resp.Receiver.DupRecv = res.Receiver.DupRecv
	resp.Receiver.AcksSent = res.Receiver.AcksSent
	resp.Receiver.FinRecv = res.Receiver.FinRecv
	resp.Receiver.DoneSent = res.Receiver.DoneSent
	resp.Receiver.StaleDrop = res.Receiver.StaleDrop
	resp.Receiver.MaxBuf = res.Receiver.MaxBuf
	if err != nil {
		resp.Error = err.Error()
		status := http.StatusInternalServerError
		if errors.Is(err, sim.ErrInvalidConfig) {
			status = http.StatusBadRequest
		}
		writeJSON(w, status, resp)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

const helpText = `UDP reliable-transfer simulator

Endpoints:
  GET  /api/health      liveness probe
  POST /api/transfers   run one simulated transfer, body is JSON:
    {
      "sizeBytes":    262144,   // file size in bytes (0..256MiB)
      "seed":         42,       // seeds content, generation and faults
      "dropRate":     0.10,     // per-packet drop probability
      "dupRate":      0.05,     // per-packet duplicate probability
      "reorderRate":  0.10,     // per-packet reorder probability
      "stalePackets": 5,        // old-generation packets injected
      "window":       16,       // sliding window in packets
      "chunkSize":    1024,     // payload bytes per packet
      "rtoMs":        50,       // retransmission timeout
      "timeoutMs":    30000,    // overall deadline
      "transport":    "udp",    // "udp" (loopback) or "mem"
      "initialSeq":   0         // first sequence number
    }
  drop+dup+reorder must be <= 0.95 (bounded loss).

Example:
  curl -s localhost:8080/api/transfers -d '{"sizeBytes":262144,"seed":42,"dropRate":0.1,"dupRate":0.05,"reorderRate":0.1,"stalePackets":5,"transport":"udp"}'
`
