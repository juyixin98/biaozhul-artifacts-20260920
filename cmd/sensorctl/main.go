// Command sensorctl is the companion CLI: it signs ingestion requests with
// real HMAC-SHA256, posts batches, runs a webhook receiver that verifies
// incoming alert signatures, and mints random secrets.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"sensorhealth/internal/cryptox"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "gen-secret":
		err = cmdGenSecret(os.Args[2:])
	case "sign":
		err = cmdSign(os.Args[2:])
	case "post":
		err = cmdPost(os.Args[2:])
	case "recv":
		err = cmdRecv(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `sensorctl — sensor health CLI

Subcommands:
  gen-secret                       Print 64 hex chars from crypto/rand.
  sign      --body FILE            Print X-Timestamp / X-Signature headers
              [--timestamp RFC3339]  for a raw request body.
  post      --url URL --secret S   Sign and POST a batch.
              --file FILE (default - = stdin)
  recv      --addr :9090 --secret S  Listen as a webhook, verify HMAC.
`)
	os.Exit(2)
}

func cmdGenSecret(args []string) error {
	fs := flag.NewFlagSet("gen-secret", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return err
	}
	s, err := cryptox.NewSecret()
	if err != nil {
		return err
	}
	fmt.Println(s)
	return nil
}

func loadFile(path string) ([]byte, error) {
	if path == "" || path == "-" {
		return io.ReadAll(os.Stdin)
	}
	return os.ReadFile(path)
}

func cmdSign(args []string) error {
	fs := flag.NewFlagSet("sign", flag.ContinueOnError)
	bodyPath := fs.String("file", "-", "body file (- for stdin)")
	secret := fs.String("secret", os.Getenv("SENSOR_INGEST_SECRET"), "HMAC secret")
	tsArg := fs.String("timestamp", "", "RFC3339 timestamp (default: now)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	body, err := loadFile(*bodyPath)
	if err != nil {
		return err
	}
	ts := time.Now().UTC()
	if *tsArg != "" {
		ts, err = time.Parse(time.RFC3339, *tsArg)
		if err != nil {
			return fmt.Errorf("parse timestamp: %w", err)
		}
	}
	fmt.Printf("X-Timestamp: %s\n", ts.Format(time.RFC3339))
	fmt.Printf("X-Signature: sha256=%s\n", cryptox.Sign(*secret, ts.Unix(), body))
	return nil
}

func cmdPost(args []string) error {
	fs := flag.NewFlagSet("post", flag.ContinueOnError)
	url := fs.String("url", envOr("SENSOR_URL", "http://127.0.0.1:8080"), "server base URL")
	secret := fs.String("secret", os.Getenv("SENSOR_INGEST_SECRET"), "HMAC ingestion secret")
	bodyPath := fs.String("file", "-", "batch JSON file (- for stdin)")
	tsArg := fs.String("timestamp", "", "RFC3339 timestamp (default: now)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *secret == "" {
		return fmt.Errorf("--secret (or SENSOR_INGEST_SECRET) is required")
	}
	body, err := loadFile(*bodyPath)
	if err != nil {
		return err
	}
	// Validate it is a batch so a malformed file is caught before signing.
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(body, &probe); err != nil {
		return fmt.Errorf("body is not valid JSON: %w", err)
	}
	ts := time.Now().UTC()
	if *tsArg != "" {
		ts, err = time.Parse(time.RFC3339, *tsArg)
		if err != nil {
			return fmt.Errorf("parse timestamp: %w", err)
		}
	}
	sig := cryptox.Sign(*secret, ts.Unix(), body)
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(*url, "/")+"/v1/ingest", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Timestamp", ts.Format(time.RFC3339))
	req.Header.Set("X-Signature", "sha256="+sig)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	fmt.Printf("HTTP %d\n%s\n", resp.StatusCode, string(respBody))
	if resp.StatusCode >= 300 {
		return fmt.Errorf("server returned %s", resp.Status)
	}
	return nil
}

func cmdRecv(args []string) error {
	fs := flag.NewFlagSet("recv", flag.ContinueOnError)
	addr := fs.String("addr", ":9090", "listen address")
	secret := fs.String("secret", os.Getenv("SENSOR_WEBHOOK_SECRET"), "HMAC webhook secret")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *secret == "" {
		return fmt.Errorf("--secret (or SENSOR_WEBHOOK_SECRET) is required")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		ts, err := time.Parse(time.RFC3339, r.Header.Get("X-Sensorhealth-Timestamp"))
		if err != nil {
			http.Error(w, "bad timestamp", http.StatusBadRequest)
			return
		}
		sig := strings.TrimPrefix(r.Header.Get("X-Sensorhealth-Signature"), "sha256=")
		// Real HMAC verification with constant-time compare.
		if !cryptox.Verify(*secret, sig, ts.Unix(), body) {
			fmt.Fprintf(os.Stderr, "[REJECT] %s invalid HMAC\n", r.RemoteAddr)
			http.Error(w, "invalid signature", http.StatusUnauthorized)
			return
		}
		fmt.Printf("[OK] %s ts=%s body=%s\n", r.RemoteAddr, ts.Format(time.RFC3339), string(body))
		w.WriteHeader(http.StatusNoContent)
	})
	fmt.Fprintf(os.Stderr, "webhook receiver listening on %s\n", *addr)
	return http.ListenAndServe(*addr, mux)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
