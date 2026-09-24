// Command inject produces the special deliveries the acceptance suite needs:
//
//	inject badjson   <id>           — non-JSON payload (→ quarantine, ACKed)
//	inject badsig    <id> [secret]  — valid JSON, wrong HMAC (→ quarantine)
//	inject unknown   <id>           — unregistered device id (→ quarantine)
//	inject retained  <id> [secret]  — a valid sample published with RETAIN
//	inject gap       <id> <boot> <seq> [secret] — arbitrary signed seq (gaps/old boot)
//
// All injections are QoS 1. They let us prove: bad payloads are isolated and
// acknowledged (never blocking other devices); retained payloads never act as
// fresh samples; seq can be forced for restart/old-boot scenarios.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"mqttredel/internal/crypto"
	"mqttredel/internal/pub"
	"mqttredel/internal/telemetry"
)

func main() {
	// Manual parsing so flags may appear in ANY position (Go's flag package
	// stops at the first non-flag argument). The first non-flag token is the
	// subcommand kind; the rest are positional arguments.
	server := env("MQTT_ADDR", "127.0.0.1:11883")
	var positional []string
	for i := 1; i < len(os.Args); i++ {
		switch {
		case os.Args[i] == "-mqtt" || os.Args[i] == "--mqtt":
			if i+1 >= len(os.Args) {
				usage()
			}
			server = os.Args[i+1]
			i++
		case strings.HasPrefix(os.Args[i], "-mqtt="):
			server = strings.TrimPrefix(os.Args[i], "-mqtt=")
		default:
			positional = append(positional, os.Args[i])
		}
	}
	if len(positional) == 0 {
		usage()
	}
	kind := positional[0]
	rest := positional[1:]
	if len(rest) == 0 {
		usage()
	}
	deviceID := rest[0]

	ctx := context.Background()
	p, err := pub.Connect(ctx, server, "inject-"+randID())
	if err != nil {
		fatal(err)
	}
	defer p.Close()

	topic := telemetry.TopicPrefix + deviceID
	secret := "secret-" + deviceID
	// A trailing non-numeric argument overrides the HMAC secret.
	if len(rest) >= 3 {
		if _, e := strconv.ParseInt(rest[2], 10, 64); e != nil {
			secret = rest[2]
		}
	}

	switch kind {
	case "badjson":
		send(ctx, p, topic, []byte(`{not-json`), false)

	case "badsig":
		send(ctx, p, topic, signedPayload(deviceID, 1, 1, 23.5, "wrong-secret"), false)

	case "unknown":
		// topic device matches payload, but device is not provisioned
		send(ctx, p, topic, signedPayload(deviceID, 1, 1, 23.5, secret), false)

	case "retained":
		send(ctx, p, topic, signedPayload(deviceID, 1, 1, 99.9, secret), true)

	case "gap":
		if len(rest) < 3 {
			usage()
		}
		boot, _ := strconv.ParseInt(rest[1], 10, 64)
		seq, _ := strconv.ParseInt(rest[2], 10, 64)
		send(ctx, p, topic, signedPayload(deviceID, boot, seq, 42.0, secret), false)

	default:
		usage()
	}
}

func signedPayload(deviceID string, boot, seq int64, value float64, secret string) []byte {
	ts := time.Now().UTC().UnixMilli()
	f := crypto.CanonicalFields{DeviceID: deviceID, BootGen: boot, Seq: seq, Value: value, TSMillis: ts}
	sig := crypto.Sign(secret, f)
	raw, _ := json.Marshal(map[string]any{
		"device_id": deviceID,
		"boot_gen":  boot,
		"seq":       seq,
		"value":     value,
		"ts_ms":     ts,
		"sig":       sig,
	})
	return raw
}

func send(ctx context.Context, p *pub.Publisher, topic string, payload []byte, retained bool) {
	if err := p.Publish(ctx, topic, payload, retained); err != nil {
		fatal(err)
	}
	fmt.Fprintf(os.Stdout, "injected retained=%v topic=%s payload=%s\n", retained, topic, string(payload))
}

func randID() string {
	b := make([]byte, 3)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  inject badjson  <id>
  inject badsig   <id> [secret]
  inject unknown  <id>
  inject retained <id> [secret]
  inject gap      <id> <boot> <seq> [secret]
flags: [-mqtt host:port]`)
	os.Exit(2)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
