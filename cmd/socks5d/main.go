// Command socks5d runs a loopback-only SOCKS5 CONNECT proxy with
// username/password auth and a small HTTP status API.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"example.com/socks5proxy/internal/socks5"
)

func main() {
	var (
		socksAddr  = flag.String("socks-addr", "127.0.0.1:1080", "SOCKS5 listen address (loopback only)")
		httpAddr   = flag.String("http-addr", "127.0.0.1:8080", "HTTP status API listen address (loopback only)")
		user       = flag.String("user", "testuser", "preset username (the only accepted one)")
		pass       = flag.String("pass", "testpass", "preset password (the only accepted one)")
		allowPorts = flag.String("allow-ports", "", "comma-separated target port whitelist, e.g. \"80,8080,9000-9010\"; empty = any port")
		extraHosts = flag.String("allow-hosts", "", "comma-separated extra allowed target hostnames; loopback IPs and localhost are always allowed")
		hsTimeout  = flag.Duration("handshake-timeout", 10*time.Second, "max duration of greeting+auth+request phase")
	)
	flag.Parse()

	ports, err := parsePorts(*allowPorts)
	if err != nil {
		log.Fatalf("invalid -allow-ports: %v", err)
	}
	var hosts []string
	if *extraHosts != "" {
		hosts = strings.Split(*extraHosts, ",")
	}

	srv := socks5.NewServer(socks5.Config{
		Username:         *user,
		Password:         *pass,
		HandshakeTimeout: *hsTimeout,
		AllowPorts:       ports,
		ExtraHosts:       hosts,
	})

	ln, err := net.Listen("tcp", *socksAddr)
	if err != nil {
		log.Fatalf("listen %s: %v", *socksAddr, err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("GET /stats", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(srv.Stats())
	})
	mux.HandleFunc("GET /whitelist", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"hosts": append([]string{"127.0.0.0/8", "::1", "localhost"}, hosts...),
			"ports": portsList(ports),
		})
	})
	httpSrv := &http.Server{Addr: *httpAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	httpLn, err := net.Listen("tcp", *httpAddr)
	if err != nil {
		log.Fatalf("listen %s: %v", *httpAddr, err)
	}

	go func() {
		log.Printf("HTTP status API on http://%s (endpoints: /healthz /stats /whitelist)", *httpAddr)
		if err := httpSrv.Serve(httpLn); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http serve: %v", err)
		}
	}()
	go func() {
		log.Printf("SOCKS5 proxy on %s (auth user=%q, whitelist: loopback targets)", *socksAddr, *user)
		if err := srv.Serve(ln); err != nil {
			log.Fatalf("socks5 serve: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Printf("shutting down")
	ln.Close()
	httpSrv.Close()
}

func parsePorts(s string) (map[uint16]bool, error) {
	if s == "" {
		return nil, nil
	}
	m := map[uint16]bool{}
	for _, part := range strings.Split(s, ",") {
		if lo, hi, ok := strings.Cut(part, "-"); ok {
			loN, err := strconv.Atoi(lo)
			if err != nil {
				return nil, err
			}
			hiN, err := strconv.Atoi(hi)
			if err != nil {
				return nil, err
			}
			if loN < 1 || hiN > 65535 || loN > hiN {
				return nil, fmt.Errorf("bad range %q", part)
			}
			for p := loN; p <= hiN; p++ {
				m[uint16(p)] = true
			}
			continue
		}
		p, err := strconv.Atoi(part)
		if err != nil || p < 1 || p > 65535 {
			return nil, fmt.Errorf("bad port %q", part)
		}
		m[uint16(p)] = true
	}
	return m, nil
}

func portsList(m map[uint16]bool) any {
	if len(m) == 0 {
		return "any"
	}
	out := []int{}
	for p := range m {
		out = append(out, int(p))
	}
	return out
}
