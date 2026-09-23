// Package httpapi 提供 DNS 代理的 HTTP 接口（纯后端，无界面）。
package httpapi

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"dnsproxy/internal/dnsmsg"
	"dnsproxy/internal/proxy"
)

// Handler 装配全部路由。
func Handler(p *proxy.Proxy) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/resolve", resolve(p))
	mux.HandleFunc("/cache", cacheView(p))
	mux.HandleFunc("/cache/flush", cacheFlush(p))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return logRequests(mux)
}

type answerJSON struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	TTL   uint32 `json:"ttl"`
	Value string `json:"value"`
}

type resolveResponse struct {
	Name    string       `json:"name"`
	Type    string       `json:"type"`
	RCode   int          `json:"rcode"`
	Answers []answerJSON `json:"answers"`
	TTL     uint32       `json:"ttl"`
	Cached  bool         `json:"cached"`
}

type errorResponse struct {
	Error string `json:"error"`
}

func resolve(p *proxy.Proxy) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
			return
		}
		name := r.URL.Query().Get("name")
		if name == "" {
			writeJSON(w, http.StatusBadRequest, errorResponse{Error: "missing required query parameter: name"})
			return
		}
		qtype := r.URL.Query().Get("type")
		if qtype == "" {
			qtype = "A"
		}
		t, err := proxy.ParseQType(qtype)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, errorResponse{Error: "unsupported type: " + qtype + " (only A/AAAA)"})
			return
		}

		ans, err := p.Resolve(r.Context(), name, t)
		if err != nil {
			var pe *proxy.Error
			if errors.As(err, &pe) {
				status := http.StatusBadGateway
				switch pe.Kind {
				case proxy.KindInvalidInput:
					status = http.StatusBadRequest
				case proxy.KindTimeout:
					status = http.StatusGatewayTimeout
				}
				writeJSON(w, status, errorResponse{Error: pe.Msg + ": " + peErrText(pe)})
				return
			}
			writeJSON(w, http.StatusInternalServerError, errorResponse{Error: err.Error()})
			return
		}

		out := resolveResponse{
			Name:    ans.Name,
			Type:    typeName(ans.QType),
			RCode:   int(ans.RCode),
			Answers: make([]answerJSON, 0, len(ans.Answers)),
			TTL:     ans.TTL,
			Cached:  ans.Cached,
		}
		for _, rr := range ans.Answers {
			out.Answers = append(out.Answers, answerJSON{
				Name:  rr.Name,
				Type:  typeName(rr.Type),
				TTL:   rr.TTL,
				Value: rr.IP.String(),
			})
		}
		writeJSON(w, http.StatusOK, out)
	}
}

func peErrText(pe *proxy.Error) string {
	if pe.Err != nil {
		return pe.Err.Error()
	}
	return ""
}

type cacheStats struct {
	Entries int `json:"entries"`
}

func cacheView(p *proxy.Proxy) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, cacheStats{Entries: p.Cache().Len()})
		default:
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
		}
	}
}

func cacheFlush(p *proxy.Proxy) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "method not allowed"})
			return
		}
		p.Cache().Flush()
		writeJSON(w, http.StatusOK, map[string]string{"status": "flushed"})
	}
}

func typeName(t uint16) string {
	switch t {
	case dnsmsg.TypeA:
		return "A"
	case dnsmsg.TypeAAAA:
		return "AAAA"
	default:
		return "TYPE" + itoa(int(t))
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func logRequests(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s %s", r.RemoteAddr, r.Method, r.URL.RequestURI())
		h.ServeHTTP(w, r)
	})
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b [20]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
