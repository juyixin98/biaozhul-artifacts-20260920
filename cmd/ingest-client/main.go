// Command ingest-client signs and posts a sensor message batch to the health
// server. It performs REAL HMAC-SHA256 computation and sends the exact file
// bytes it signed, so signature and payload can never drift.
//
// Usage:
//
//	go run ./cmd/ingest-client -file examples/batch_healthy.json
//	go run ./cmd/ingest-client -device temp-1 -heartbeat
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"sensorhealth/internal/crypto"
)

func main() {
	baseURL := flag.String("url", envOr("BASE_URL", "http://localhost:8080"), "server base URL")
	secret := flag.String("secret", envOr("HMAC_SECRET", "dev-shared-secret"), "shared HMAC secret")
	file := flag.String("file", "", "path to a JSON envelope file")
	device := flag.String("device", "", "device id for inline heartbeat mode")
	heartbeat := flag.Bool("heartbeat", false, "send a single heartbeat for -device")
	flag.Parse()

	var raw []byte
	switch {
	case *file != "":
		b, err := os.ReadFile(*file)
		if err != nil {
			fatal("read file: %v", err)
		}
		raw = b
	case *heartbeat && *device != "":
		raw = mustJSON(map[string]any{
			"device_id": *device,
			"messages": []map[string]any{
				{"kind": "heartbeat", "sampled_at": time.Now().UTC().Format(time.RFC3339Nano)},
			},
		})
	default:
		fatal("provide -file or -device X -heartbeat", nil)
	}

	// Validate it is a proper envelope before signing.
	var probe map[string]any
	if err := json.Unmarshal(raw, &probe); err != nil {
		fatal("invalid JSON envelope: %v", err)
	}

	ts := time.Now().UTC()
	nonce := randomNonce()
	sig := crypto.Sign([]byte(*secret), http.MethodPost, "/api/v1/ingest", ts, raw)

	req, err := http.NewRequest(http.MethodPost, *baseURL+"/api/v1/ingest", bytes.NewReader(raw))
	if err != nil {
		fatal("build request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Timestamp", ts.Format(time.RFC3339Nano))
	req.Header.Set("X-Nonce", nonce)
	req.Header.Set("X-Signature", sig)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fatal("send: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	fmt.Printf("HTTP %d\n%s\n", resp.StatusCode, string(body))
	if resp.StatusCode >= 300 {
		os.Exit(1)
	}
}

func randomNonce() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		fatal("marshal: %v", err)
	}
	return b
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func fatal(format string, err error) {
	if err != nil {
		fmt.Fprintf(os.Stderr, format+"\n", err)
	} else {
		fmt.Fprintln(os.Stderr, format)
	}
	os.Exit(1)
}
