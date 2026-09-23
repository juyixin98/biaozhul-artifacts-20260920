package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"rollout/internal/api"
	"rollout/internal/metrics"
	"rollout/internal/store"
	"rollout/internal/stub"
)

// 端到端测试：真实 PostgreSQL + 真实指标桩（httptest）+ 真实 HTTP 往返。
// 需要可连接的测试库；不可达时跳过。

type rolloutJSON struct {
	ID                 int64  `json:"id"`
	Name               string `json:"name"`
	Service            string `json:"service"`
	Status             string `json:"status"`
	StageIdx           int    `json:"stage_idx"`
	StageWeight        int    `json:"stage_weight"`
	Generation         int64  `json:"generation"`
	ThresholdVersionID int64  `json:"threshold_version_id"`
	Threshold          struct {
		MaxErrorRate    float64 `json:"max_error_rate"`
		MaxP95LatencyMs float64 `json:"max_p95_latency_ms"`
	} `json:"threshold"`
	MinSamples               int64 `json:"min_samples"`
	ObservationWindowSeconds int64 `json:"observation_window_seconds"`
}

type decisionJSON struct {
	ID            int64    `json:"id"`
	Verdict       string   `json:"verdict"`
	Reason        string   `json:"reason"`
	Samples       int64    `json:"samples"`
	ErrorRate     *float64 `json:"error_rate"`
	P95LatencyMs  *float64 `json:"p95_latency_ms"`
	Coverage      float64  `json:"coverage"`
	MetricsStatus string   `json:"metrics_status"`
	Buckets       []struct {
		Start    string  `json:"start"`
		Samples  int64   `json:"samples"`
		Errors   int64   `json:"errors"`
		P95      float64 `json:"p95_latency_ms"`
		Ingested string  `json:"ingested_at"`
	} `json:"buckets"`
	WindowComplete bool `json:"window_complete"`
}

type commandResp struct {
	Rollout  rolloutJSON   `json:"rollout"`
	Result   string        `json:"result"`
	Detail   string        `json:"detail"`
	Decision *decisionJSON `json:"decision"`
}

type evalResp struct {
	Rollout  rolloutJSON  `json:"rollout"`
	Decision decisionJSON `json:"decision"`
}

type env struct {
	t      *testing.T
	server *httptest.Server
	stub   *httptest.Server
	st     *store.Store
}

func newEnv(t *testing.T) *env {
	t.Helper()
	dbURL := os.Getenv("TEST_DATABASE_URL")
	if dbURL == "" {
		dbURL = "postgres://rollout:rollout@localhost:5432/rollout_test?sslmode=disable"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	st, err := store.Open(ctx, dbURL)
	if err != nil {
		t.Skipf("test database unavailable: %v", err)
	}
	t.Cleanup(st.Close)
	if err := st.ApplyMigrations(context.Background(), "../../migrations/0001_init.sql"); err != nil {
		t.Fatalf("migrations: %v", err)
	}
	if _, err := st.Pool.Exec(context.Background(),
		`TRUNCATE decisions, commands, rollouts, threshold_versions RESTART IDENTITY CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}

	stubSrv := httptest.NewServer(stub.New().Router())
	t.Cleanup(stubSrv.Close)

	srv := &api.Server{Store: st, Metrics: metrics.New(stubSrv.URL), Now: time.Now}
	apiSrv := httptest.NewServer(api.NewRouter(srv))
	t.Cleanup(apiSrv.Close)
	return &env{t: t, server: apiSrv, stub: stubSrv, st: st}
}

func (e *env) do(method, path string, body any, out any) (int, http.Header) {
	e.t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, e.server.URL+path, rdr)
	if err != nil {
		e.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			e.t.Fatalf("decode %s %s: %v", method, path, err)
		}
	}
	return resp.StatusCode, resp.Header
}

// 场景打在桩服务上，走 e.stub 直连
func (e *env) setScenarioOnStub(service, name string, bucketSec, correctionDelaySec int64) {
	e.t.Helper()
	b, _ := json.Marshal(map[string]any{
		"service": service, "name": name,
		"bucket_seconds": bucketSec, "correction_delay_seconds": correctionDelaySec,
	})
	resp, err := http.Post(e.stub.URL+"/scenario", "application/json", bytes.NewReader(b))
	if err != nil {
		e.t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("set scenario status %d", resp.StatusCode)
	}
}

func (e *env) createRollout(name, service string, minSamples, windowSec int64) rolloutJSON {
	e.t.Helper()
	var r rolloutJSON
	code, _ := e.do(http.MethodPost, "/rollouts", map[string]any{
		"name": name, "service": service,
		"min_samples": minSamples, "observation_window_seconds": windowSec,
		"max_error_rate": 0.05, "max_p95_latency_ms": 500,
	}, &r)
	if code != http.StatusCreated {
		e.t.Fatalf("create rollout: %d %+v", code, r)
	}
	if r.Generation != 0 || r.StageIdx != 0 || r.StageWeight != 5 || r.Status != "active" {
		e.t.Fatalf("unexpected new rollout: %+v", r)
	}
	return r
}

func (e *env) evaluate(id int64) (int, evalResp) {
	e.t.Helper()
	var out evalResp
	code, _ := e.do(http.MethodPost, fmt.Sprintf("/rollouts/%d/evaluate", id), nil, &out)
	return code, out
}

func (e *env) command(id int64, typ, key string, gen int64) (int, http.Header, commandResp) {
	e.t.Helper()
	var out commandResp
	code, hdr := e.do(http.MethodPost, fmt.Sprintf("/rollouts/%d/commands", id), map[string]any{
		"type": typ, "idempotency_key": key, "expected_generation": gen,
	}, &out)
	return code, hdr, out
}

func sleep(seconds float64) { time.Sleep(time.Duration(seconds * float64(time.Second))) }

// 健康服务走完 5%→20%→50%→100% 四个阶段并 completed
func TestHealthyRolloutPromotesThroughAllStages(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-healthy", "healthy", 1, 0)
	r := e.createRollout("happy-path", "svc-healthy", 100, 1)

	// 窗口未走完：应到桶为 0 或窗口未完成 → 不允许推进
	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK {
		t.Fatalf("evaluate: %d", code)
	}
	if ev.Decision.Verdict == "promote" {
		t.Fatalf("must not promote before window elapsed: %+v", ev.Decision)
	}

	gen := int64(0)
	for stage := 0; stage < 4; stage++ {
		sleep(1.5) // 等观察窗口走完且桶闭合
		code, ev := e.evaluate(r.ID)
		if code != http.StatusOK || ev.Decision.Verdict != "promote" {
			t.Fatalf("stage %d: expected promote verdict, got %d %+v", stage, code, ev.Decision)
		}
		code, _, cmd := e.command(r.ID, "promote", fmt.Sprintf("promote-%d", stage), gen)
		if code != http.StatusOK || cmd.Result != "applied" {
			t.Fatalf("stage %d promote: %d %+v", stage, code, cmd)
		}
		gen++
	}
	var final rolloutJSON
	e.do(http.MethodGet, fmt.Sprintf("/rollouts/%d", r.ID), nil, &final)
	if final.Status != "completed" || final.StageWeight != 100 || final.Generation != 4 {
		t.Fatalf("final rollout: %+v", final)
	}
}

// 短暂尖峰：单桶 8% 错误 + 900ms，聚合后仍在阈值内 → 可推进，证据含尖峰桶
func TestTransientSpikeDoesNotBlockPromotion(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-spike", "transient_spike", 1, 0)
	r := e.createRollout("spike", "svc-spike", 100, 4)
	sleep(4.5)

	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "promote" {
		t.Fatalf("verdict: %d %+v", code, ev.Decision)
	}
	if ev.Decision.ErrorRate == nil || *ev.Decision.ErrorRate != 0.02 {
		t.Fatalf("aggregated error rate should be 2%%, got %+v", ev.Decision.ErrorRate)
	}
	var sawSpike bool
	for _, b := range ev.Decision.Buckets {
		if b.Errors == 8 && b.P95 == 900 {
			sawSpike = true
		}
	}
	if !sawSpike {
		t.Fatalf("evidence must contain the spike bucket: %+v", ev.Decision.Buckets)
	}
	code, _, cmd := e.command(r.ID, "promote", "spike-promote", 0)
	if code != http.StatusOK || cmd.Result != "applied" || cmd.Rollout.StageIdx != 1 {
		t.Fatalf("promote after spike: %d %+v", code, cmd)
	}
}

// 持续退化：violation，推进被拒；随后可暂停、回退
func TestSustainedDegradationBlocksAndRollsBack(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-degraded", "sustained_degradation", 1, 0)
	r := e.createRollout("degraded", "svc-degraded", 100, 2)
	sleep(2.5)

	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "violation" || ev.Decision.Reason != "error_rate_exceeded" {
		t.Fatalf("verdict: %d %+v", code, ev.Decision)
	}
	code, _, cmd := e.command(r.ID, "promote", "deg-promote", 0)
	if code != http.StatusConflict || cmd.Result != "rejected_state" {
		t.Fatalf("promote must be rejected: %d %+v", code, cmd)
	}
	code, _, cmd = e.command(r.ID, "pause", "deg-pause", 0)
	if code != http.StatusOK || cmd.Result != "applied" || cmd.Rollout.Status != "paused" {
		t.Fatalf("pause: %d %+v", code, cmd)
	}
	code, _, cmd = e.command(r.ID, "rollback", "deg-rollback", 1)
	if code != http.StatusOK || cmd.Result != "applied" || cmd.Rollout.Status != "rolled_back" {
		t.Fatalf("rollback: %d %+v", code, cmd)
	}
	// 已回退后再次回退：状态拒绝
	code, _, cmd = e.command(r.ID, "rollback", "deg-rollback-2", 2)
	if code != http.StatusConflict || cmd.Result != "rejected_state" {
		t.Fatalf("second rollback: %d %+v", code, cmd)
	}
}

// 指标中断：判未知而非健康，推进被拒
func TestMetricsOutageIsUnknownNotHealthy(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-outage", "metrics_outage", 1, 0)
	r := e.createRollout("outage", "svc-outage", 100, 2)
	sleep(2.5)

	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "unknown" || ev.Decision.MetricsStatus != "missing" {
		t.Fatalf("verdict: %d %+v", code, ev.Decision)
	}
	if ev.Decision.ErrorRate != nil || ev.Decision.P95LatencyMs != nil {
		t.Fatalf("unknown must not carry healthy-looking metrics: %+v", ev.Decision)
	}
	code, _, cmd := e.command(r.ID, "promote", "outage-promote", 0)
	if code != http.StatusConflict || cmd.Result != "rejected_state" {
		t.Fatalf("promote during outage: %d %+v", code, cmd)
	}

	// 完全无场景的服务同样判未知
	r2 := e.createRollout("no-scenario", "svc-nothing", 100, 2)
	sleep(2.5)
	code, ev = e.evaluate(r2.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "unknown" {
		t.Fatalf("no-scenario verdict: %d %+v", code, ev.Decision)
	}
}

// 样本不足：hold，推进被拒
func TestInsufficientSamplesHolds(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-thin", "insufficient_samples", 1, 0)
	r := e.createRollout("thin", "svc-thin", 100, 2)
	sleep(2.5)

	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "hold" || ev.Decision.Reason != "insufficient_samples" {
		t.Fatalf("verdict: %d %+v", code, ev.Decision)
	}
	if ev.Decision.Samples >= 100 {
		t.Fatalf("samples should be below min: %+v", ev.Decision)
	}
	code, _, cmd := e.command(r.ID, "promote", "thin-promote", 0)
	if code != http.StatusConflict || cmd.Result != "rejected_state" {
		t.Fatalf("promote with thin samples: %d %+v", code, cmd)
	}
}

// 命令幂等：重试不重复执行；同键不同体被拒；代次过期被拒
func TestCommandsAreIdempotentAndGenerationChecked(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-idem", "healthy", 1, 0)
	r := e.createRollout("idem", "svc-idem", 100, 1)

	// 暂停（gen 0 → 1）
	code, _, cmd := e.command(r.ID, "pause", "pause-1", 0)
	if code != http.StatusOK || cmd.Result != "applied" || cmd.Rollout.Generation != 1 {
		t.Fatalf("pause: %d %+v", code, cmd)
	}
	// 重试同一命令：返回已存结果，不重复执行（代次不变）
	code, hdr, cmd2 := e.command(r.ID, "pause", "pause-1", 0)
	if code != http.StatusOK || hdr.Get("Idempotent-Replay") != "true" || cmd2.Rollout.Generation != 1 {
		t.Fatalf("replay: %d hdr=%v %+v", code, hdr, cmd2)
	}
	// 同一幂等键、不同请求体：SHA-256 指纹不匹配 → 409
	code, _ = e.do(http.MethodPost, fmt.Sprintf("/rollouts/%d/commands", r.ID), map[string]any{
		"type": "rollback", "idempotency_key": "pause-1", "expected_generation": 0,
	}, nil)
	if code != http.StatusConflict {
		t.Fatalf("key reuse with different body must be 409, got %d", code)
	}
	// 代次过期：期望 0 但当前 1 → 409 rejected_conflict
	code, _, cmd3 := e.command(r.ID, "rollback", "rb-stale", 0)
	if code != http.StatusConflict || cmd3.Result != "rejected_conflict" {
		t.Fatalf("stale generation: %d %+v", code, cmd3)
	}
	// 正确代次回退成功
	code, _, cmd4 := e.command(r.ID, "rollback", "rb-ok", 1)
	if code != http.StatusOK || cmd4.Result != "applied" || cmd4.Rollout.Generation != 2 {
		t.Fatalf("rollback: %d %+v", code, cmd4)
	}
	// 已回退的发布不能推进
	code, _, cmd5 := e.command(r.ID, "promote", "promote-after-rb", 2)
	if code != http.StatusConflict || cmd5.Result != "rejected_state" {
		t.Fatalf("promote after rollback: %d %+v", code, cmd5)
	}
}

// 推进命令重试不重复推进阶段
func TestPromoteRetryDoesNotDoubleAdvance(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-retry", "healthy", 1, 0)
	r := e.createRollout("retry", "svc-retry", 100, 1)
	sleep(1.5)

	code, _, cmd := e.command(r.ID, "promote", "promote-x", 0)
	if code != http.StatusOK || cmd.Rollout.StageIdx != 1 || cmd.Rollout.Generation != 1 {
		t.Fatalf("promote: %d %+v", code, cmd)
	}
	code, hdr, cmd2 := e.command(r.ID, "promote", "promote-x", 0)
	if code != http.StatusOK || hdr.Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay: %d %+v", code, cmd2)
	}
	if cmd2.Rollout.StageIdx != 1 || cmd2.Rollout.Generation != 1 {
		t.Fatalf("retry must not advance again: %+v", cmd2.Rollout)
	}
}

// 回退后迟到成功：首版数据导致回退，订正数据到达后同一窗口判定转为健康
func TestLateSuccessAfterRollback(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-late", "late_success", 1, 6) // 订正延迟 6s
	r := e.createRollout("late", "svc-late", 100, 2)
	sleep(2.5)

	// 首版数据：10% 错误率 → violation → 回退
	code, ev := e.evaluate(r.ID)
	if code != http.StatusOK || ev.Decision.Verdict != "violation" {
		t.Fatalf("initial verdict: %d %+v", code, ev.Decision)
	}
	code, _, cmd := e.command(r.ID, "rollback", "late-rb", 0)
	if code != http.StatusOK || cmd.Result != "applied" {
		t.Fatalf("rollback: %d %+v", code, cmd)
	}

	// 等订正数据到达（场景开始后 6s）
	sleep(4.5)
	code, ev2 := e.evaluate(r.ID)
	if code != http.StatusOK {
		t.Fatalf("late evaluate: %d", code)
	}
	if ev2.Decision.Verdict != "promote" || ev2.Decision.ErrorRate == nil || *ev2.Decision.ErrorRate != 0 {
		t.Fatalf("late corrected data should be healthy: %+v", ev2.Decision)
	}

	// 决策日志按时间顺序呈现 violation → promote（迟到成功证据）
	var decisions []decisionJSON
	e.do(http.MethodGet, fmt.Sprintf("/rollouts/%d/decisions", r.ID), nil, &decisions)
	if len(decisions) < 2 {
		t.Fatalf("expected >=2 decisions, got %d", len(decisions))
	}
	// 列表倒序：最新在前
	if decisions[0].Verdict != "promote" {
		t.Fatalf("latest decision should be late-success promote: %+v", decisions[0])
	}
	if decisions[1].Verdict != "violation" {
		t.Fatalf("decision log must retain the pre-rollback violation: %+v", decisions[1])
	}
}

// 阈值版本在发布开始时冻结：之后插入新阈值版本不影响进行中的发布
func TestThresholdFrozenAtRolloutStart(t *testing.T) {
	e := newEnv(t)
	e.setScenarioOnStub("svc-freeze", "healthy", 1, 0)
	r := e.createRollout("freeze", "svc-freeze", 100, 1)
	frozenTV := r.ThresholdVersionID

	// 模拟运营侧发布新阈值版本（更严格）
	if _, err := e.st.Pool.Exec(context.Background(),
		`INSERT INTO threshold_versions (max_error_rate, max_p95_latency_ms, note) VALUES (0.001, 50, 'stricter v2')`); err != nil {
		t.Fatal(err)
	}

	var got rolloutJSON
	e.do(http.MethodGet, fmt.Sprintf("/rollouts/%d", r.ID), nil, &got)
	if got.ThresholdVersionID != frozenTV {
		t.Fatalf("threshold version changed mid-rollout: %d -> %d", frozenTV, got.ThresholdVersionID)
	}
	if got.Threshold.MaxErrorRate != 0.05 || got.Threshold.MaxP95LatencyMs != 500 {
		t.Fatalf("threshold not frozen: %+v", got.Threshold)
	}
}
