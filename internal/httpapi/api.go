// Package httpapi exposes the broker's operational surface over HTTP
// using only net/http. The MQTT protocol itself runs over raw TCP; these
// endpoints inject messages and inspect sessions for demos and tests.
package httpapi

import (
	"encoding/json"
	"net/http"
	"strings"

	"mqttsub/internal/broker"
)

// Handler builds the HTTP mux for a broker.
func Handler(b *broker.Broker) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", index)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "use GET")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "use GET")
			return
		}
		writeJSON(w, http.StatusOK, b.Stats())
	})
	mux.HandleFunc("/publish", publish(b))
	mux.HandleFunc("/sessions", sessions(b))
	mux.HandleFunc("/sessions/", sessionItem(b))
	return mux
}

const indexPage = `mqttsub — MQTT 3.1.1 subset control API

MQTT clients connect over TCP (separate port) with CONNECT/SUBSCRIBE/PUBLISH(QoS1)/PUBACK.
These HTTP endpoints are for injecting messages and inspecting sessions:

  GET  /healthz
  GET  /stats
  POST /publish                 {"topic":"a/b","qos":1,"payload":"hello"}
  GET  /sessions                list sessions
  GET  /sessions/{clientID}     one session
  DELETE /sessions/{clientID}  purge a durable session (closes its connection)

Example:
  curl -s -XPOST localhost:8081/publish -d '{"topic":"sensors/1","qos":1,"payload":"42"}'
`

func index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		writeError(w, http.StatusNotFound, "not found; see GET /")
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write([]byte(indexPage))
}

type publishReq struct {
	Topic   string          `json:"topic"`
	QoS     byte            `json:"qos"`
	Payload json.RawMessage `json:"payload"`
}

func publish(b *broker.Broker) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "use POST")
			return
		}
		var req publishReq
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		payload, err := decodePayload(req.Payload)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		matched, err := b.Publish(req.Topic, req.QoS, payload)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{
			"topic":            req.Topic,
			"qos":              req.QoS,
			"bytes":            len(payload),
			"matched_sessions": matched,
		})
	}
}

// decodePayload accepts a JSON string (UTF-8) or an explicit base64
// {"base64":"..."} object; null/absent means empty.
func decodePayload(raw json.RawMessage) ([]byte, error) {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil, nil
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return []byte(s), nil
	}
	var b64 struct {
		Base64 string `json:"base64"`
	}
	if err := json.Unmarshal(raw, &b64); err == nil && b64.Base64 != "" {
		return decodeBase64(b64.Base64)
	}
	return nil, errBadPayload
}

var errBadPayload = badRequest("payload must be a JSON string or {\"base64\":\"...\"}")

func sessions(b *broker.Broker) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "use GET")
			return
		}
		writeJSON(w, http.StatusOK, b.Sessions())
	}
}

func sessionItem(b *broker.Broker) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimPrefix(r.URL.Path, "/sessions/")
		if id == "" || strings.Contains(id, "/") {
			writeError(w, http.StatusBadRequest, "expected /sessions/{clientID}")
			return
		}
		switch r.Method {
		case http.MethodGet:
			info, ok := b.Session(id)
			if !ok {
				writeError(w, http.StatusNotFound, "no such session: "+id)
				return
			}
			writeJSON(w, http.StatusOK, info)
		case http.MethodDelete:
			if !b.DeleteSession(id) {
				writeError(w, http.StatusNotFound, "no such session: "+id)
				return
			}
			writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
		default:
			writeError(w, http.StatusMethodNotAllowed, "use GET or DELETE")
		}
	}
}

type apiErr struct{ msg string }

func (e *apiErr) Error() string { return e.msg }

func badRequest(msg string) error { return &apiErr{msg: msg} }

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
