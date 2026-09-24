// Command device simulates a real telemetry device:
//
//   - owns a provisioned HMAC secret and signs every payload for real
//     (HMAC-SHA256 over a canonical byte string);
//   - numbers samples 1..N within a boot generation;
//   - "-restart" bumps boot_gen and resets seq to 1, demonstrating that the
//     backend never confuses seq spaces across device reboots;
//   - publishes QoS 1 (optionally RETAINED) to telemetry/<deviceID>.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"time"

	"mqttredel/internal/crypto"
	"mqttredel/internal/pub"
	"mqttredel/internal/telemetry"
)

func main() {
	var (
		server   = flag.String("mqtt", env("MQTT_ADDR", "127.0.0.1:11883"), "broker host:port")
		deviceID = flag.String("id", "dev-001", "device id (also topic suffix)")
		secret   = flag.String("secret", env("DEVICE_SECRET", ""), "HMAC secret; defaults to dev-<id>-secret")
		boot     = flag.Int64("boot", 1, "boot generation; bump on device restart")
		count    = flag.Int("count", 5, "number of samples to send (0 = run forever at -interval)")
		startSeq = flag.Int64("seq-start", 1, "first seq number in this boot")
		interval = flag.Duration("interval", 200*time.Millisecond, "interval between samples")
		retained = flag.Bool("retained", false, "publish with RETAIN flag")
	)
	flag.Parse()

	if *secret == "" {
		*secret = "secret-" + *deviceID
	}

	ctx := context.Background()
	p, err := pub.Connect(ctx, *server, "sim-"+*deviceID+"-"+randomSuffix())
	if err != nil {
		fatal(err)
	}
	defer p.Close()

	topic := telemetry.TopicPrefix + *deviceID
	seq := *startSeq
	for i := 0; *count == 0 || i < *count; i++ {
		now := time.Now().UTC()
		m := struct {
			DeviceID string  `json:"device_id"`
			BootGen  int64   `json:"boot_gen"`
			Seq      int64   `json:"seq"`
			Value    float64 `json:"value"`
			TSMillis int64   `json:"ts_ms"`
			Sig      string  `json:"sig"`
		}{
			DeviceID: *deviceID,
			BootGen:  *boot,
			Seq:      seq,
			Value:    float64(2000+rand.Intn(500)) / 10,
			TSMillis: now.UnixMilli(),
		}
		f := crypto.CanonicalFields{
			DeviceID: m.DeviceID, BootGen: m.BootGen, Seq: m.Seq,
			Value: m.Value, TSMillis: m.TSMillis,
		}
		m.Sig = crypto.Sign(*secret, f)
		raw, _ := json.Marshal(m)
		if err := p.Publish(ctx, topic, raw, *retained); err != nil {
			fatal(err)
		}
		fmt.Fprintf(os.Stdout, "published %s boot=%d seq=%d value=%.1f retained=%v\n",
			topic, *boot, seq, m.Value, *retained)
		seq++
		if *count == 0 || i < *count-1 {
			time.Sleep(*interval)
		}
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func randomSuffix() string {
	return fmt.Sprintf("%04d", rand.Intn(10000))
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
