// Command gen-examples writes the example payload files in examples/ with
// signatures genuinely computed via HMAC-SHA256 (never hand-typed).
//
//	go run ./examples/gen
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"mqttredel/internal/crypto"
)

type payload struct {
	DeviceID string  `json:"device_id"`
	BootGen  int64   `json:"boot_gen"`
	Seq      int64   `json:"seq"`
	Value    float64 `json:"value"`
	TSMillis int64   `json:"ts_ms"`
	Sig      string  `json:"sig"`
}

func writeFile(dir, name, secret string, p payload) {
	p.Sig = crypto.Sign(secret, crypto.CanonicalFields{
		DeviceID: p.DeviceID, BootGen: p.BootGen, Seq: p.Seq,
		Value: p.Value, TSMillis: p.TSMillis,
	})
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), append(raw, '\n'), 0o644); err != nil {
		panic(err)
	}
	fmt.Printf("wrote %s (sig=%s...)\n", name, p.Sig[:16])
}

func main() {
	dir := "."
	if _, err := os.Stat("examples"); err == nil {
		dir = "examples"
	}
	writeFile(dir, "payload-good.json", "secret-dev-001", payload{
		DeviceID: "dev-001", BootGen: 1, Seq: 1, Value: 213.7, TSMillis: 1727100000123,
	})
	writeFile(dir, "payload-good-seq2.json", "secret-dev-001", payload{
		DeviceID: "dev-001", BootGen: 1, Seq: 2, Value: 214.1, TSMillis: 1727100001123,
	})
	writeFile(dir, "payload-restart-boot2.json", "secret-dev-001", payload{
		DeviceID: "dev-001", BootGen: 2, Seq: 1, Value: 214.9, TSMillis: 1727100060000,
	})
}
