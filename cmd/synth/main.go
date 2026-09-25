// Command synth posts synthetic metric samples to a running server to
// exercise the alert state machine. Four scenarios are provided:
//
//	jitter   (default) values bounce around the threshold — pending must
//	          never fire while streaks are shorter than trigger_for
//	gap       a clean firing followed by a long silence -> nodata -> resume
//	outoforder a batch containing timestamps before the virtual clock, plus
//	          an unordered batch the server must sort
//	band      value hysteresis band demo using recovery_threshold
//
// All timestamps are virtual: the generator starts at the server clock (or
// -start unix seconds) and advances it through sample timestamps.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

type sample struct {
	Metric string      `json:"metric"`
	TS     interface{} `json:"ts"`
	Value  float64     `json:"value"`
}

type ingestBody struct {
	Samples []sample `json:"samples"`
}

func main() {
	base := flag.String("url", "http://127.0.0.1:8080", "server base URL")
	scenario := flag.String("scenario", "jitter", "jitter|gap|outoforder|band")
	start := flag.Int64("start", 0, "virtual start as Unix seconds (default: server clock)")
	step := flag.Duration("step", 20*time.Second, "seconds between generated samples")
	flag.Parse()

	var t0 time.Time
	if *start > 0 {
		t0 = time.Unix(*start, 0).UTC()
	} else {
		t0 = serverClock(*base)
	}
	// Begin one step after clock to be unambiguous.
	t0 = t0.Add(*step)

	switch *scenario {
	case "jitter":
		runJitter(*base, t0, *step)
	case "gap":
		runGap(*base, t0, *step)
	case "outoforder":
		runOutOfOrder(*base, t0, *step)
	case "band":
		runBand(*base, t0, *step)
	default:
		fmt.Fprintf(os.Stderr, "unknown scenario %q\n", *scenario)
		os.Exit(2)
	}
}

func serverClock(base string) time.Time {
	resp, err := http.Get(base + "/clock")
	if err != nil {
		fatal("get server clock (is it running?): %v", err)
	}
	defer resp.Body.Close()
	var env struct {
		Data struct {
			Now string `json:"now"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		fatal("decode clock: %v", err)
	}
	t, err := time.Parse(time.RFC3339Nano, env.Data.Now)
	if err != nil {
		fatal("parse clock %q: %v", env.Data.Now, err)
	}
	return t.UTC()
}

func post(path string, body interface{}) []byte {
	buf, err := json.Marshal(body)
	if err != nil {
		fatal("marshal: %v", err)
	}
	resp, err := http.Post(path, "application/json", bytes.NewReader(buf))
	if err != nil {
		fatal("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		fatal("POST %s -> %s: %s", path, resp.Status, string(data))
	}
	return data
}

func tick(base string, d time.Duration) {
	post(base+"/clock/tick", map[string]string{"duration": d.String()})
}

func send(base string, metric string, t0 time.Time, offset time.Duration, value float64) {
	ts := t0.Add(offset)
	body := ingestBody{Samples: []sample{{Metric: metric, TS: ts.Unix(), Value: value}}}
	raw := post(base+"/samples", body)
	var env struct {
		Data struct {
			Results []struct {
				Duplicate bool `json:"duplicate"`
				Late      bool `json:"late"`
				Events    []struct {
					Type    string `json:"type"`
					From    string `json:"from"`
					To      string `json:"to"`
					Message string `json:"message"`
				} `json:"events"`
			} `json:"results"`
		} `json:"data"`
	}
	_ = json.Unmarshal(raw, &env)
	tag := ""
	for _, r := range env.Data.Results {
		if r.Duplicate {
			tag += " duplicate"
		}
		if r.Late {
			tag += " LATE"
		}
		for _, ev := range r.Events {
			tag += fmt.Sprintf(" [%s: %s->%s %s]", ev.Type, ev.From, ev.To, ev.Message)
		}
	}
	fmt.Printf("  sample %s t=%s v=%g%s\n", metric, ts.Format("15:04:05"), value, tag)
}

func createRule(base string, r map[string]interface{}) {
	post(base+"/rules", r)
}

// jitter: values alternate around the threshold so no hot streak reaches
// trigger_for; then a sustained breach fires; brief dips into the warm zone
// do not resolve until a sustained cold streak.
func runJitter(base string, t0 time.Time, step time.Duration) {
	fmt.Println("== scenario: jitter ==")
	createRule(base, map[string]interface{}{
		"id": "cpu-jitter", "metric": "cpu_usage", "operator": ">=",
		"threshold": 80, "trigger_for": "60s", "recover_for": "90s",
		"no_data_for": "2m", "recovery_threshold": 70,
	})
	// 90 hot would start a streak, 75 (warm) cancels it; repeat 4 times
	// within 260s — never reaches 60s sustained hot.
	values := []float64{90, 75, 90, 75, 90, 75, 90, 75, 90, 90, 90, 90, 90, 75, 75, 75, 75, 75, 60, 60, 60, 60}
	for i, v := range values {
		send(base, "cpu_usage", t0, time.Duration(i)*step, v)
	}
}

// gap: fire, then stop sending samples and tick well past no_data_for, then
// resume with a healthy value.
func runGap(base string, t0 time.Time, step time.Duration) {
	fmt.Println("== scenario: gap ==")
	createRule(base, map[string]interface{}{
		"id": "mem-gap", "metric": "mem_usage", "operator": ">",
		"threshold": 90, "trigger_for": "30s", "recover_for": "30s",
		"no_data_for": "1m",
	})
	send(base, "mem_usage", t0, 0, 95)
	send(base, "mem_usage", t0, step, 96)
	send(base, "mem_usage", t0, 2*step, 97)
	// Last contact at t0+2step; tick 2m30s crosses no_data_for=1m while
	// staying before the first resume sample at t0+3m+step.
	fmt.Printf("  ticking %s without samples ...\n", 150*time.Second)
	tick(base, 150*time.Second)
	send(base, "mem_usage", t0, 3*time.Minute+step, 50)
	send(base, "mem_usage", t0, 3*time.Minute+2*step, 40)
}

// outoforder: drive the clock forward with in-order samples, then submit a
// batch whose timestamps lie in the past and one intentionally unordered
// future batch that the server must sort.
func runOutOfOrder(base string, t0 time.Time, step time.Duration) {
	fmt.Println("== scenario: outoforder ==")
	createRule(base, map[string]interface{}{
		"id": "qps-ooo", "metric": "qps", "operator": ">",
		"threshold": 1000, "trigger_for": "20s", "recover_for": "20s",
		"no_data_for": "1m",
	})
	send(base, "qps", t0, 0, 100)
	send(base, "qps", t0, step, 100)

	// Unordered future batch (sent t3,t1,t2 — server sorts to t1,t2,t3).
	future := t0.Add(4 * step)
	sendRaw(base, ingestBody{Samples: []sample{
		{Metric: "qps", TS: future.Add(2 * step).Unix(), Value: 1500},
		{Metric: "qps", TS: future.Unix(), Value: 1200},
		{Metric: "qps", TS: future.Add(step).Unix(), Value: 1400},
	}})
	fmt.Println("  sent unordered future batch t+80s,t+60s,t+70s (server sorts)")

	// Late batch: timestamps in the past. Stored + marked late, no events.
	sendRaw(base, ingestBody{Samples: []sample{
		{Metric: "qps", TS: t0.Add(step / 2).Unix(), Value: 9999},
		{Metric: "qps", TS: t0.Unix(), Value: 9998}, // same ts as an existing sample -> duplicate+late
	}})
	fmt.Println("  sent late batch with one duplicate timestamp")
}

// band: with recovery_threshold=70, values 70..80 hold a firing alert open
// (warm); only values <=70 start the recovery countdown.
func runBand(base string, t0 time.Time, step time.Duration) {
	fmt.Println("== scenario: band ==")
	createRule(base, map[string]interface{}{
		"id": "temp-band", "metric": "temperature", "operator": ">=",
		"threshold": 80, "trigger_for": "30s", "recover_for": "60s",
		"no_data_for": "2m", "recovery_threshold": 70,
	})
	vals := []float64{85, 86, 87, 76, 74, 78, 72, 65, 64, 63}
	for i, v := range vals {
		send(base, "temperature", t0, time.Duration(i)*step, v)
	}
}

// sendRaw posts a batch and prints the raw response.
func sendRaw(base string, body ingestBody) {
	raw := post(base+"/samples", body)
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, raw, "", "  "); err == nil {
		fmt.Println("  response:", pretty.String())
	}
}

func fatal(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "synth: "+format+"\n", args...)
	os.Exit(1)
}
