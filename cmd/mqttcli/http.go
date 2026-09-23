package main

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// publishViaHTTP injects a QoS1 PUBLISH through the control API.
func publishViaHTTP(base, topic, payload string) {
	body := fmt.Sprintf(`{"topic":%q,"qos":1,"payload":%q}`, topic, payload)
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(base, "/")+"/publish", bytes.NewBufferString(body))
	if err != nil {
		fatalf("http request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	cli := &http.Client{Timeout: 3 * time.Second}
	resp, err := cli.Do(req)
	if err != nil {
		fatalf("http publish: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusAccepted {
		fatalf("http publish status %d: %s", resp.StatusCode, data)
	}
	fmt.Printf("HTTP POST /publish -> %s: %s\n", topic, strings.TrimSpace(string(data)))
}
