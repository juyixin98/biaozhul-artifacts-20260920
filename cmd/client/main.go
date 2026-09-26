// Command client drives the shutdown demo service through fault-injection
// scenarios and emits structured JSON results. Run a fresh `server` for each
// scenario (see examples/run-demo.sh); every scenario ends with shutdown.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"io"
	"os"
	"sync"
	"time"

	"gracefulshutdown/internal/client"
)

// Result is the structured output of one scenario run.
type Result struct {
	Scenario     string               `json:"scenario"`
	StartedAt    time.Time            `json:"startedAt"`
	FinishedAt   time.Time            `json:"finishedAt"`
	Observations []client.Observation `json:"observations"`
	Probes       []client.ProbeResult `json:"probes"`
	ServerReport json.RawMessage      `json:"serverReport,omitempty"`
	Summary      map[string]any       `json:"summary"`
	Errors       []string             `json:"errors,omitempty"`
}

func main() {
	base := flag.String("base", "http://127.0.0.1:18080", "traffic base URL")
	admin := flag.String("admin", "http://127.0.0.1:18081", "admin base URL")
	scenario := flag.String("scenario", "drain", "scenario: drain|cancel|repeat|reject")
	out := flag.String("out", "", "write JSON result to this path (stdout always gets a copy)")
	flag.Parse()

	c := client.New(*base, *admin)
	ctx := context.Background()

	var res Result
	switch *scenario {
	case "drain":
		res = scenarioDrain(ctx, c)
	case "cancel":
		res = scenarioCancel(ctx, c)
	case "repeat":
		res = scenarioRepeat(ctx, c)
	case "reject":
		res = scenarioReject(ctx, c)
	default:
		os.Stderr.WriteString("unknown scenario: " + *scenario + "\n")
		os.Exit(2)
	}

	encoded, err := json.MarshalIndent(res, "", "  ")
	if err != nil {
		os.Stderr.WriteString("encode: " + err.Error() + "\n")
		os.Exit(1)
	}
	os.Stdout.Write(append(encoded, '\n'))
	if *out != "" {
		if werr := os.WriteFile(*out, append(encoded, '\n'), 0o644); werr != nil {
			os.Stderr.WriteString("write out: " + werr.Error() + "\n")
			os.Exit(1)
		}
	}
}

// recorder collects observations and probes concurrently.
type recorder struct {
	mu  sync.Mutex
	res *Result
}

func newRec(name string) *recorder {
	return &recorder{res: &Result{Scenario: name, StartedAt: time.Now(), Summary: map[string]any{}}}
}

func (r *recorder) addObs(o client.Observation) {
	r.mu.Lock()
	r.res.Observations = append(r.res.Observations, o)
	r.mu.Unlock()
}

func (r *recorder) addProbe(p client.ProbeResult) {
	r.mu.Lock()
	r.res.Probes = append(r.res.Probes, p)
	r.mu.Unlock()
}

func (r *recorder) finish(report json.RawMessage) Result {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.res.FinishedAt = time.Now()
	if json.Valid(report) {
		r.res.ServerReport = report
	} else if len(report) > 0 {
		r.res.Errors = append(r.res.Errors, "trigger returned non-JSON: "+string(report))
	}
	tally := map[string]int{}
	for _, o := range r.res.Observations {
		key := o.Action + ":" + o.Outcome
		tally[key]++
	}
	r.res.Summary["observationTally"] = tally
	r.res.Summary["totalObservations"] = len(r.res.Observations)
	r.res.Summary["totalProbes"] = len(r.res.Probes)
	return *r.res
}

// pollProbes samples ready/live until stop closes.
func pollProbes(ctx context.Context, c *client.Client, r *recorder, stop <-chan struct{}) {
	t := time.NewTicker(50 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ctx.Done():
			return
		case <-t.C:
			if p, err := c.Probe(ctx, "livez"); err == nil {
				r.addProbe(p)
			}
			if p, err := c.Probe(ctx, "readyz"); err == nil {
				r.addProbe(p)
			}
		}
	}
}

// scenarioDrain: short dependency latency + generous budget => every accepted
// long request completes during DRAINING; readiness flips before liveness.
func scenarioDrain(ctx context.Context, c *client.Client) Result {
	r := newRec("drain")
	if _, err := c.SetFault(ctx, 300*time.Millisecond, false, false); err != nil {
		r.res.Errors = append(r.res.Errors, err.Error())
	}

	stop := make(chan struct{})
	go pollProbes(ctx, c, r, stop)

	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			o, _ := c.SendWork(ctx)
			r.addObs(o)
		}()
	}
	time.Sleep(150 * time.Millisecond) // let the requests become in-flight

	report, obs, _ := c.TriggerShutdown(ctx, 1)
	r.addObs(obs)
	wg.Wait()
	close(stop)

	res := r.finish(report)
	res.Summary["expectation"] = "all long requests complete during DRAINING; readiness fails before liveness"
	return res
}

// scenarioCancel: dependency hangs; drain budget elapses, phase 3 cancels the
// long request, the stream gets an explicit cancelled event and the background
// job is cancelled too.
func scenarioCancel(ctx context.Context, c *client.Client) Result {
	r := newRec("cancel")
	if _, err := c.SetFault(ctx, 0, false, true); err != nil { // hang
		r.res.Errors = append(r.res.Errors, err.Error())
	}

	stop := make(chan struct{})
	go pollProbes(ctx, c, r, stop)

	var wg sync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); o, _ := c.SendWork(ctx); r.addObs(o) }()
	go func() { defer wg.Done(); o, _ := c.OpenStream(ctx, 30*time.Second); r.addObs(o) }()
	go func() {
		defer wg.Done()
		o, _ := c.SpawnBackground(ctx, 30*time.Second)
		r.addObs(o)
	}()
	time.Sleep(200 * time.Millisecond)

	report, obs, _ := c.TriggerShutdown(ctx, 1)
	r.addObs(obs)
	wg.Wait()
	close(stop)

	res := r.finish(report)
	res.Summary["expectation"] = "hung long request + stream + background job are explicitly cancelled after drain budget"
	return res
}

// scenarioRepeat: one hung request and three signals at once; duplicate
// signals are counted and escalate the waits instead of crashing.
func scenarioRepeat(ctx context.Context, c *client.Client) Result {
	r := newRec("repeat")
	if _, err := c.SetFault(ctx, 0, false, true); err != nil {
		r.res.Errors = append(r.res.Errors, err.Error())
	}

	stop := make(chan struct{})
	go pollProbes(ctx, c, r, stop)

	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); o, _ := c.SendWork(ctx); r.addObs(o) }()
	time.Sleep(200 * time.Millisecond)

	report, obs, _ := c.TriggerShutdown(ctx, 3)
	r.addObs(obs)
	wg.Wait()
	close(stop)

	res := r.finish(report)
	res.Summary["expectation"] = "3 signals recorded; repeats escalate drain/cancel waits; shutdown still ends in CLOSED"
	return res
}

// scenarioReject: once shutdown starts, new foreground and background work is
// rejected with 503 and no background task is spawned.
func scenarioReject(ctx context.Context, c *client.Client) Result {
	r := newRec("reject")
	if _, err := c.SetFault(ctx, 0, false, true); err != nil {
		r.res.Errors = append(r.res.Errors, err.Error())
	}

	// One hung request keeps shutdown in DRAINING.
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); o, _ := c.SendWork(ctx); r.addObs(o) }()
	time.Sleep(200 * time.Millisecond)

	// Trigger in a separate goroutine; meanwhile observe 503s.
	var report json.RawMessage
	triggerDone := make(chan struct{})
	go func() {
		rep, obs, _ := c.TriggerShutdown(ctx, 1)
		r.addObs(obs)
		report = rep
		close(triggerDone)
	}()

	waitUntilNotRunning(ctx, c)

	// Post-cut attempts: must be rejected, and must not spawn a job.
	if o, _ := c.SendWork(ctx); true {
		r.addObs(o)
	}
	if o, _ := c.SpawnBackground(ctx, 5*time.Second); true {
		r.addObs(o)
	}
	// One more duplicate signal while draining.
	func() {
		resp, err := c.HTTP.Post(c.AdminURL+"/trigger-shutdown?signals=1", "application/json", nil)
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
	}()

	<-triggerDone
	wg.Wait()

	res := r.finish(report)
	res.Summary["expectation"] = "new work and background spawn return 503 after STOP_ACCEPT; report shows rejected counts"
	return res
}

// waitUntilNotRunning polls readiness until it reports a non-RUNNING phase.
func waitUntilNotRunning(ctx context.Context, c *client.Client) {
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		p, err := c.Probe(ctx, "readyz")
		if err == nil && p.StatusCode == 503 {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
}
