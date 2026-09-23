// Command synthload 向运行中的 server 发送合成高基数攻击流量：
// 先建立 --budget 个“稳定组合”（低基数基座），再发送大量带唯一标签的
// “攻击样本”。配合极小 series-budget 的 server，可演示内存有界与 overflow。
//
// 例：
//
//	synthload -addr=127.0.0.1:8080 -metric=http_requests -budget=50 -attack=200000 -batch=2000
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "server address")
	metric := flag.String("metric", "http_requests", "metric name")
	budget := flag.Int("budget", 50, "number of stable low-cardinality series to establish")
	attack := flag.Int("attack", 100000, "number of unique-label attack samples")
	batch := flag.Int("batch", 1000, "samples per HTTP request")
	valueMax := flag.Int("value-max", 0, "if >0, generate long label values up to this many bytes (truncation demo)")
	flag.Parse()

	if *batch <= 0 {
		log.Fatal("--batch must be > 0")
	}
	url := fmt.Sprintf("http://%s/ingest", *addr)
	client := &http.Client{Timeout: 30 * time.Second}

	// 1) 基座：budget 个稳定组合，每个重复 2 次，证明“老组合”持续累加。
	log.Printf("phase 1: establish %d stable series (x2 samples each)", *budget)
	stableLabels := make([]map[string]string, 0, *budget)
	for i := 0; i < *budget; i++ {
		stableLabels = append(stableLabels, map[string]string{
			"route": fmt.Sprintf("/api/v1/resource/%02d", i%10),
			"pod":   fmt.Sprintf("pod-%04d", i),
			"zone":  []string{"a", "b", "c"}[i%3],
		})
	}
	for range 2 {
		samples := make([]map[string]any, 0, *budget)
		for _, l := range stableLabels {
			samples = append(samples, map[string]any{"metric": *metric, "labels": l, "value": 1.0})
		}
		if err := postBatch(client, url, samples); err != nil {
			log.Fatal(err)
		}
	}

	// 2) 攻击：唯一标签（request_id），每个样本一个全新组合。
	log.Printf("phase 2: fire %d unique-label attack samples in batches of %d", *attack, *batch)
	sent := 0
	for sent < *attack {
		n := *batch
		if sent+n > *attack {
			n = *attack - sent
		}
		samples := make([]map[string]any, 0, n)
		for j := 0; j < n; j++ {
			labels := map[string]string{
				"route":      "/api/v1/login",
				"request_id": fmt.Sprintf("req-%d-%d", sent+j, time.Now().UnixNano()),
			}
			if *valueMax > 0 {
				labels["blob"] = randomBlob(*valueMax, sent+j)
			}
			samples = append(samples, map[string]any{"metric": *metric, "labels": labels, "value": 1.0})
		}
		if err := postBatch(client, url, samples); err != nil {
			log.Fatalf("batch at offset %d: %v", sent, err)
		}
		sent += n
		if sent%(10**batch) == 0 || sent == *attack {
			log.Printf("  sent %d/%d attack samples", sent, *attack)
		}
	}

	// 3) 再补一轮基座样本，证明老组合仍在、未被挤走。
	base := make([]map[string]any, 0, len(stableLabels))
	for _, l := range stableLabels {
		base = append(base, map[string]any{"metric": *metric, "labels": l, "value": 1.0})
	}
	if err := postBatch(client, url, base); err != nil {
		log.Fatal(err)
	}
	log.Printf("phase 3: re-sent %d stable samples; query GET /stats and GET /metrics/%s to verify", len(base), *metric)
}

func postBatch(client *http.Client, url string, samples []map[string]any) error {
	body, err := json.Marshal(map[string]any{"samples": samples})
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted {
		return fmt.Errorf("unexpected status %d: %s", resp.StatusCode, raw)
	}
	return nil
}

// randomBlob 生成确定性的长字符串（避免每次运行引入随机源依赖）。
func randomBlob(maxLen, seed int) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, 0, maxLen)
	for i := 0; i < maxLen; i++ {
		b = append(b, alphabet[(seed*31+i)%len(alphabet)])
	}
	return string(b)
}
