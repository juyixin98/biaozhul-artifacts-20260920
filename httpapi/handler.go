// Package httpapi exposes the subset broker's management surface over HTTP,
// built only on the standard library net/http. It is intentionally separate
// from the MQTT listener: MQTT runs over raw TCP, these endpoints are for
// inspection and for injecting publications when no real MQTT publisher is
// available (which is how the acceptance demos can be driven with curl).
package httpapi

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strings"

	"mqttsubset/mqtt"
)

type apiError struct {
	Error string `json:"error"`
}

// Handler builds the HTTP handler tree backed by b.
func Handler(b *mqtt.Broker, logger *log.Logger) http.Handler {
	if logger == nil {
		logger = log.Default()
	}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	mux.HandleFunc("GET /v1/sessions", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"sessions": b.Sessions()})
	})

	mux.HandleFunc("GET /v1/sessions/{clientID}", func(w http.ResponseWriter, r *http.Request) {
		s := findSession(b, r.PathValue("clientID"))
		if s == nil {
			writeJSON(w, http.StatusNotFound, apiError{Error: "session not found"})
			return
		}
		writeJSON(w, http.StatusOK, s)
	})

	mux.HandleFunc("DELETE /v1/sessions/{clientID}", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("clientID")
		if !b.DeleteSession(id) {
			writeJSON(w, http.StatusNotFound, apiError{Error: "session not found"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
	})

	// POST /v1/publish injects an application message as if sent by an MQTT
	// client (subject to the same delivery/QoS rules). Body is JSON:
	//
	//	{"topic":"a/b","qos":1,"payload_text":"hi"}
	//	{"topic":"a/b","qos":1,"payload_base64":"aGVsbG8="}
	mux.HandleFunc("POST /v1/publish", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Topic         string `json:"topic"`
			QoS           int    `json:"qos"`
			PayloadText   string `json:"payload_text"`
			PayloadBase64 string `json:"payload_base64"`
		}
		body := http.MaxBytesReader(w, r.Body, 1<<20)
		dec := json.NewDecoder(body)
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, apiError{Error: "invalid JSON body: " + err.Error()})
			return
		}
		if req.Topic == "" {
			writeJSON(w, http.StatusBadRequest, apiError{Error: "topic is required"})
			return
		}
		if req.QoS < 0 || req.QoS > 1 {
			writeJSON(w, http.StatusBadRequest, apiError{Error: "this subset supports qos 0 or 1 only"})
			return
		}
		var payload []byte
		switch {
		case req.PayloadBase64 != "":
			bp, err := base64.StdEncoding.DecodeString(req.PayloadBase64)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, apiError{Error: "invalid payload_base64"})
				return
			}
			payload = bp
		default:
			payload = []byte(req.PayloadText)
		}
		matched, err := b.Publish(req.Topic, byte(req.QoS), payload)
		if err != nil {
			if errors.Is(err, mqtt.ErrProtocol) {
				writeJSON(w, http.StatusBadRequest, apiError{Error: "invalid topic"})
				return
			}
			writeJSON(w, http.StatusInternalServerError, apiError{Error: err.Error()})
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{
			"topic":               req.Topic,
			"qos":                 req.QoS,
			"matched_subscribers": matched,
			"delivered":           matched,
			"note":                "at-least-once: QoS 1 messages are retried until PUBACK; no end-to-end exactly-once guarantee",
		})
	})

	// Friendly index with the request catalogue.
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeJSON(w, http.StatusNotFound, apiError{Error: "not found"})
			return
		}
		io.WriteString(w, strings.TrimSpace(indexDoc)+"\n")
	})

	return logRequests(logger, mux)
}

func findSession(b *mqtt.Broker, id string) *mqtt.SessionInfo {
	sessions := b.Sessions()
	for i := range sessions {
		if sessions[i].ClientID == id {
			return &sessions[i]
		}
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func logRequests(logger *log.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		logger.Printf("%s %s", r.Method, r.URL.RequestURI())
		h.ServeHTTP(w, r)
	})
}

const indexDoc = `{
  "service": "mqtt-311-subset",
  "mqtt": "raw TCP, MQTT 3.1.1 subset (CONNECT/PUBLISH QoS0-1/PUBACK/SUBSCRIBE/UNSUBSCRIBE/PINGREQ/DISCONNECT)",
  "http_endpoints": {
    "GET  /healthz": "liveness probe",
    "GET  /v1/sessions": "list sessions with subscriptions and delivery counters",
    "GET  /v1/sessions/{clientID}": "inspect one session",
    "DELETE /v1/sessions/{clientID}": "delete a session (disconnects live client, removes durable state)",
    "POST /v1/publish": "publish a message: {\"topic\":...,\"qos\":0|1,\"payload_text\":...} or payload_base64"
  }
}`
