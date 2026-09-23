// Command socks5proxy runs a loopback-only SOCKS5 CONNECT proxy with
// username/password authentication and a destination allowlist, plus a small
// net/http control plane (/healthz, /stats, /allowlist).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"syscall"
	"time"

	"socks5loop/internal/httpapi"
	"socks5loop/internal/socks5"
)

func main() {
	listenAddr := flag.String("listen", "127.0.0.1:1080", "SOCKS5 listen address (loopback only)")
	httpAddr := flag.String("http", "127.0.0.1:8080", "HTTP control-plane address (loopback only)")
	username := flag.String("username", "", "required SOCKS5 username")
	password := flag.String("password", "", "required SOCKS5 password")
	allowCIDRs := flag.String("allow-cidrs", "", "comma-separated extra destination CIDRs (added to defaults)")
	allowHosts := flag.String("allow-hosts", "", "comma-separated extra destination host suffixes (added to defaults)")
	handshakeTimeout := flag.Duration("handshake-timeout", 10*time.Second, "deadline for method/auth/request negotiation")
	dialTimeout := flag.Duration("dial-timeout", 10*time.Second, "deadline for dialing the target")
	flag.Parse()

	// Credentials may also come from the environment; the command-line flag
	// wins. Empty credentials are refused: anonymous SOCKS5 is never allowed.
	user := firstNonEmpty(*username, os.Getenv("SOCKS5_USER"))
	pass := firstNonEmpty(*password, os.Getenv("SOCKS5_PASS"))
	if user == "" || pass == "" {
		fmt.Fprintln(os.Stderr, "error: --username/--password (or SOCKS5_USER/SOCKS5_PASS) are required")
		os.Exit(2)
	}

	cidrs := appendDefault(socks5.DefaultAllowCIDRs, *allowCIDRs)
	hosts := appendDefault(socks5.DefaultAllowHosts, *allowHosts)
	allow, err := socks5.ParseAllowList(cidrs, hosts)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error: "+err.Error())
		os.Exit(2)
	}

	srv, err := socks5.New(socks5.Config{
		Username:         user,
		Password:         pass,
		Allow:            allow,
		HandshakeTimeout: *handshakeTimeout,
		DialTimeout:      *dialTimeout,
		Logger:           slog.Default(),
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "error: "+err.Error())
		os.Exit(2)
	}

	ln, err := srv.Listen("tcp", *listenAddr)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error: socks listen: "+err.Error())
		os.Exit(1)
	}
	slog.Info("SOCKS5 proxy listening (loopback only)", "addr", ln.Addr())

	mux := httpapi.Handler(srv, cidrs, hosts)
	httpLn, err := loopbackListen("tcp", *httpAddr)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error: http listen: "+err.Error())
		os.Exit(1)
	}
	httpSrv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	slog.Info("HTTP control plane listening (loopback only)", "addr", httpLn.Addr())

	serveErr := make(chan error, 2)
	go func() { serveErr <- srv.Serve(ln) }()
	go func() { serveErr <- httpSrv.Serve(httpLn) }()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	select {
	case <-ctx.Done():
		slog.Info("shutting down")
	case err := <-serveErr:
		if err != nil {
			fmt.Fprintln(os.Stderr, "fatal: "+err.Error())
			os.Exit(1)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutdownCtx)
	_ = ln.Close()
}

// loopbackListen refuses to bind anything that does not resolve to loopback.
func loopbackListen(network, addr string) (net.Listener, error) {
	ln, err := net.Listen(network, addr)
	if err != nil {
		return nil, err
	}
	ta, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		_ = ln.Close()
		return nil, fmt.Errorf("refusing to listen on non-TCP address %q", addr)
	}
	a, ok := netip.AddrFromSlice(ta.IP)
	if !ok || !a.Unmap().IsLoopback() {
		_ = ln.Close()
		return nil, fmt.Errorf("refusing to listen on non-loopback address %q", addr)
	}
	return ln, nil
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func appendDefault(def []string, csv string) []string {
	out := append([]string(nil), def...)
	for _, item := range splitCSV(csv) {
		out = append(out, item)
	}
	return out
}

func splitCSV(s string) []string {
	var out []string
	cur := ""
	for _, r := range s {
		switch r {
		case ',', ' ', '\t':
			if cur != "" {
				out = append(out, cur)
				cur = ""
			}
		default:
			cur += string(r)
		}
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}
