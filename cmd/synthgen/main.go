// Command synthgen generates synthetic observability traffic against a
// running server. It supports three scenarios:
//
//	normal   - a small, stable set of label combinations (legit traffic)
//	attack   - a high-cardinality attack: every sample carries a unique
//	           request_id, attempting to blow the per-metric series budget
//	truncate - label values far over the length limit, exercising truncation
//
// All scenarios can be mixed into one run; the point is to let an operator
// watch /api/v1/stats while an attack is in progress.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"cardinalitybudget/internal/api"
	"cardinalitybudget/internal/store"
)

func main() {
	var (
		addr    = flag.String("addr", "http://127.0.0.1:8080", "server base URL")
		mode    = flag.String("mode", "attack", "normal | attack | truncate")
		metric  = flag.String("metric", "http_requests", "metric name")
		waves   = flag.Int("waves", 50, "number of ingest batches")
		batch   = flag.Int("batch", 200, "samples per batch")
		workers = flag.Int("workers", 8, "concurrent ingest workers")
		stableN = flag.Int("stable-combos", 50, "normal mode: number of stable combinations")
		seed    = flag.Int64("seed", 1, "random seed")
	)
	flag.Parse()

	if *batch > api.MaxSamplesPerRequest {
		*batch = api.MaxSamplesPerRequest
	}

	var gen sampleGenerator
	baseLabels := func(i int) map[string]string {
		// A realistic stable label set shared by all scenarios.
		return map[string]string{
			"method": []string{"GET", "POST", "PUT", "DELETE"}[i%4],
			"path":   []string{"/a", "/b", "/c", "/healthz"}[i%4],
			"status": []string{"200", "404", "500"}[i%3],
			"host":   "host-" + fmt.Sprintf("%02d", i%6),
		}
	}
	switch *mode {
	case "normal":
		gen = func(rng *rand.Rand, global uint64) store.Sample {
			i := rng.Intn(*stableN)
			return store.Sample{Metric: *metric, Labels: baseLabels(i), Value: rng.Float64() * 100}
		}
	case "attack":
		gen = func(rng *rand.Rand, global uint64) store.Sample {
			l := baseLabels(int(global) % *stableN)
			// The hostile label: globally unique per sample.
			l["request_id"] = fmt.Sprintf("req-%d-%d", time.Now().UnixNano(), global)
			return store.Sample{Metric: *metric, Labels: l, Value: rng.Float64() * 100}
		}
	case "truncate":
		gen = func(rng *rand.Rand, global uint64) store.Sample {
			l := baseLabels(int(global) % *stableN)
			l["user_agent"] = strings.Repeat("x", 5000) + "-" + fmt.Sprintf("%d", global)
			return store.Sample{Metric: *metric, Labels: l, Value: 1}
		}
	default:
		log.Fatalf("unknown mode %q", *mode)
	}

	var counter uint64
	var accepted, rejected, overflow int64
	client := &http.Client{Timeout: 10 * time.Second}

	var wg sync.WaitGroup
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(*seed + int64(worker)))
			for wave := 0; wave < *waves; wave++ {
				samples := make([]store.Sample, *batch)
				for i := range samples {
					samples[i] = gen(rng, atomic.AddUint64(&counter, 1))
				}
				body, err := json.Marshal(map[string]any{"samples": samples})
				if err != nil {
					log.Printf("marshal: %v", err)
					return
				}
				resp, err := client.Post(*addr+"/api/v1/ingest", "application/json", bytes.NewReader(body))
				if err != nil {
					log.Printf("post: %v", err)
					time.Sleep(time.Second)
					continue
				}
				var out struct {
					Accepted int `json:"accepted"`
					Rejected int `json:"rejected"`
					Overflow int `json:"overflow"`
				}
				raw, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				if resp.StatusCode != http.StatusOK {
					log.Printf("status %d: %s", resp.StatusCode, raw)
					continue
				}
				if err := json.Unmarshal(raw, &out); err != nil {
					log.Printf("decode: %v", err)
					continue
				}
				atomic.AddInt64(&accepted, int64(out.Accepted))
				atomic.AddInt64(&rejected, int64(out.Rejected))
				atomic.AddInt64(&overflow, int64(out.Overflow))
			}
		}(w)
	}
	wg.Wait()

	fmt.Printf("mode=%s sent=%d accepted=%d rejected=%d overflow=%d\n",
		*mode, counter, accepted, rejected, overflow)
	if err := printStats(client, *addr, *metric); err != nil {
		log.Printf("stats: %v", err)
	}
}

type sampleGenerator func(rng *rand.Rand, global uint64) store.Sample

func printStats(client *http.Client, addr, metric string) error {
	resp, err := client.Get(addr + "/api/v1/stats")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	fmt.Printf("stats: %s\n", string(raw))

	resp2, err := client.Get(addr + "/api/v1/metrics/" + metric)
	if err != nil {
		return err
	}
	defer resp2.Body.Close()
	raw2, _ := io.ReadAll(resp2.Body)
	fmt.Printf("metric: %s\n", string(raw2))
	return nil
}
