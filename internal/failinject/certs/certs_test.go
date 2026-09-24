package certs

import (
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestEnsureCerts_RealChainAndTLSHandshake(t *testing.T) {
	dir := t.TempDir()
	dns := []string{"config-distributor-webhook.config-system.svc", "localhost"}
	if err := EnsureCerts(dir, dns, []net.IP{net.ParseIP("127.0.0.1")}); err != nil {
		t.Fatalf("EnsureCerts: %v", err)
	}

	caPEM, err := os.ReadFile(filepath.Join(dir, CACertName))
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		t.Fatal("generated CA is not parseable PEM")
	}

	cert, err := tls.LoadX509KeyPair(
		filepath.Join(dir, ServerCertName),
		filepath.Join(dir, ServerKeyName))
	if err != nil {
		t.Fatalf("load server keypair: %v", err)
	}

	// Serve TLS with the generated certificate.
	ts := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	ts.TLS = &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}
	ts.StartTLS()
	defer ts.Close()

	// Client trusts ONLY the generated CA and verifies the service DNS SAN.
	client := &http.Client{
		Timeout: 3 * time.Second,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{
				RootCAs: roots,
				// Dial to the loopback but validate the service name.
				ServerName: dns[0],
				MinVersion: tls.VersionTLS12,
			},
		},
	}
	// httptest listens on 127.0.0.1; ServerName mismatch with dial host is OK
	// because we set ServerName explicitly, and the cert has 127.0.0.1 IP SAN.
	resp, err := client.Get(ts.URL)
	if err != nil {
		t.Fatalf("TLS handshake with real generated cert failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}

	// Second Ensure must reuse, not regenerate.
	info1, _ := os.Stat(filepath.Join(dir, ServerCertName))
	time.Sleep(10 * time.Millisecond)
	if err := EnsureCerts(dir, dns, []net.IP{net.ParseIP("127.0.0.1")}); err != nil {
		t.Fatal(err)
	}
	info2, _ := os.Stat(filepath.Join(dir, ServerCertName))
	if !info1.ModTime().Equal(info2.ModTime()) {
		t.Fatal("existing certs must be reused, not overwritten")
	}
}
