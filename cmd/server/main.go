package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"tailsampler/internal/api"
	"tailsampler/internal/sampler"
)

func main() {
	var (
		addr          = flag.String("addr", ":8080", "HTTP listen address")
		decisionWait  = flag.Duration("decision-wait", 10*time.Second, "tail-sampling decision wait window")
		latencyThresh = flag.Int64("latency-threshold-ms", 500, "keep traces at least this slow (ms)")
		budgetPerMin  = flag.Int("budget-keeps-per-min", 100, "max kept traces per rolling minute (0 = unlimited)")
		maxInflight   = flag.Int("max-inflight-traces", 10000, "max undecided traces; excess are force-decided (degraded)")
		decisionTTL   = flag.Duration("decision-ttl", 0, "how long decisions stay queryable/consistent (default 10x decision-wait)")
		dataDir       = flag.String("data-dir", "data", "persistence directory for decisions.jsonl/spans.jsonl (empty = memory only)")
		tick          = flag.Duration("tick", 500*time.Millisecond, "how often due traces are decided")
	)
	flag.Parse()

	cfg := sampler.Config{
		DecisionWait:       *decisionWait,
		LatencyThresholdMs: *latencyThresh,
		BudgetKeepsPerMin:  *budgetPerMin,
		MaxInflightTraces:  *maxInflight,
		DecisionTTL:        *decisionTTL,
	}
	store, err := sampler.NewStore(*dataDir)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer store.Close()

	s := sampler.New(cfg, store, nil)
	if *dataDir != "" {
		prior, err := sampler.LoadDecisions(*dataDir)
		if err != nil {
			log.Fatalf("load decisions: %v", err)
		}
		restored := s.LoadDecisions(prior)
		log.Printf("restored %d of %d persisted decisions from %s (decision-ttl applies)", restored, len(prior), *dataDir)
	}

	go func() {
		t := time.NewTicker(*tick)
		defer t.Stop()
		for range t.C {
			s.DecideDue()
		}
	}()

	log.Printf("tailsampler listening on %s (decision-wait=%s latency>=%dms budget=%d/min max-inflight=%d data-dir=%q)",
		*addr, cfg.DecisionWait, cfg.LatencyThresholdMs, cfg.BudgetKeepsPerMin, cfg.MaxInflightTraces, *dataDir)
	log.Fatal(http.ListenAndServe(*addr, api.NewServer(s)))
}
