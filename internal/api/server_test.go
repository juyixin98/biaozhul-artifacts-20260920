package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"cgroup-analyzer/internal/analysis"
	"cgroup-analyzer/internal/fixture"
	"cgroup-analyzer/internal/store"
)

const fixtureRoot = "../../fixtures"

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	insts, err := fixture.Load(fixtureRoot)
	if err != nil {
		t.Fatalf("load fixtures: %v", err)
	}
	if err := st.ReplaceInstances(insts); err != nil {
		t.Fatalf("ingest: %v", err)
	}
	return httptest.NewServer(NewServer(st, fixtureRoot).Router())
}

func get(t *testing.T, url string, wantStatus int) []byte {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != wantStatus {
		t.Fatalf("GET %s: want %d, got %d", url, wantStatus, resp.StatusCode)
	}
	var buf []byte
	buf = make([]byte, 0, 4096)
	tmp := make([]byte, 4096)
	for {
		n, err := resp.Body.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if err != nil {
			break
		}
	}
	return buf
}

func TestListContainers(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	body := get(t, srv.URL+"/api/containers", http.StatusOK)
	var out struct {
		Instances []store.InstanceSummary `json:"instances"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(out.Instances) != 3 {
		t.Fatalf("want 3 instances, got %d: %s", len(out.Instances), body)
	}
	// Same container name "web" must appear as two distinct instances.
	web := map[string]bool{}
	for _, in := range out.Instances {
		if in.Container == "web" {
			web[in.Instance] = true
		}
	}
	if !web["boot-1"] || !web["boot-2"] {
		t.Fatalf("web instances missing: %v", web)
	}
}

func TestReportHandComputed(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	body := get(t, srv.URL+"/api/containers/web/instances/boot-1/report", http.StatusOK)
	var rep analysis.Report
	if err := json.Unmarshal(body, &rep); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if rep.SampleCount != 6 || len(rep.Intervals) != 5 {
		t.Fatalf("unexpected report shape: %+v", rep)
	}
	iv := rep.Intervals[0]
	if iv.CPUCores == nil || *iv.CPUCores != 0.5 {
		t.Fatalf("want 0.5 cores, got %v", iv.CPUCores)
	}
	if iv.MemUsageRatioEnd == nil || *iv.MemUsageRatioEnd != 0.390625 {
		t.Fatalf("want mem ratio 0.390625, got %v", iv.MemUsageRatioEnd)
	}
	var sawOOM bool
	for _, e := range rep.Events {
		if e.Type == "oom_kill" {
			sawOOM = true
			if len(e.Evidence) == 0 {
				t.Fatal("oom_kill without evidence")
			}
		}
	}
	if !sawOOM {
		t.Fatalf("want oom_kill event: %+v", rep.Events)
	}
}

func TestRatesAndEventsEndpoints(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	body := get(t, srv.URL+"/api/containers/batch/instances/run-1/rates", http.StatusOK)
	var rates struct {
		Intervals []analysis.IntervalRate `json:"intervals"`
	}
	if err := json.Unmarshal(body, &rates); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(rates.Intervals) != 3 {
		t.Fatalf("want 3 intervals, got %d", len(rates.Intervals))
	}
	// The reset interval must carry no rate (never negative).
	if rates.Intervals[1].CPUCores != nil || !rates.Intervals[1].CounterReset {
		t.Fatalf("reset interval wrong: %+v", rates.Intervals[1])
	}

	body = get(t, srv.URL+"/api/containers/batch/instances/run-1/events", http.StatusOK)
	var evs struct {
		Events []analysis.Event `json:"events"`
	}
	if err := json.Unmarshal(body, &evs); err != nil {
		t.Fatalf("decode: %v", err)
	}
	types := map[string]bool{}
	for _, e := range evs.Events {
		types[e.Type] = true
	}
	for _, want := range []string{"sample_gap", "counter_reset", "memory_max_hit", "normal_exit"} {
		if !types[want] {
			t.Fatalf("missing event type %q in %+v", want, evs.Events)
		}
	}
	if types["oom_kill"] {
		t.Fatalf("batch/run-1 must not be classified as OOM: %+v", evs.Events)
	}
}

func TestNotFound(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	get(t, srv.URL+"/api/containers/ghost/instances/none/report", http.StatusNotFound)
}

func TestIngestEndpoint(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()
	resp, err := http.Post(srv.URL+"/api/ingest", "application/json", nil)
	if err != nil {
		t.Fatalf("POST ingest: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("ingest status %d", resp.StatusCode)
	}
	var out struct {
		Instances int `json:"instances"`
		Samples   int `json:"samples"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out.Instances != 3 || out.Samples != 13 {
		t.Fatalf("want 3 instances / 13 samples, got %+v", out)
	}
}
