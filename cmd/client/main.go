// Command idemcheck 是"幂等响应保存"项目的故障注入验收客户端。
//
// 它会：
//  1. 编译并以子进程方式拉起业务服务（假时钟）与独立的假外部审计系统；
//  2. 按预设场景发起并发重试、提交前/后断线、进程崩溃重启、外部重复等验证；
//  3. 输出结构化 JSON 报告（请求、断言、账本/外部系统证据）。
//
// 不接任何生产系统。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"runtime"
	"time"
)

func main() {
	work := flag.String("work", "", "工作目录（默认临时目录）")
	out := flag.String("out", "report.json", "结构化报告输出路径（- 表示 stdout）")
	ttl := flag.Duration("ttl", 5*time.Second, "占位 TTL（传给服务器）")
	waitMax := flag.Duration("wait-max", 1*time.Second, "处理中重复请求等待上限")
	keep := flag.Bool("keep", false, "保留工作目录（默认结束删除）")
	flag.Parse()

	workDir := *work
	if workDir == "" {
		var err error
		workDir, err = os.MkdirTemp("", "idemcheck-")
		if err != nil {
			fatal(err)
		}
	} else {
		_ = os.MkdirAll(workDir, 0o755)
	}
	if !*keep && *work == "" {
		defer func() { _ = os.RemoveAll(workDir) }()
	}

	report := runAcceptance(workDir, *ttl, *waitMax)
	payload, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		fatal(err)
	}
	if *out == "-" {
		fmt.Println(string(payload))
	} else {
		if err := os.WriteFile(*out, append(payload, '\n'), 0o644); err != nil {
			fatal(err)
		}
		fmt.Printf("report written to %s\n", *out)
	}

	if report.Summary.Failed > 0 {
		os.Exit(1)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "idemcheck:", err)
	os.Exit(2)
}

// ---- 报告结构 ----

type Report struct {
	GeneratedAt string        `json:"generated_at"`
	GoVersion   string        `json:"go_version"`
	TTL         string        `json:"pending_ttl"`
	WaitMax     string        `json:"wait_max"`
	WorkDir     string        `json:"work_dir"`
	Scenarios   []ScenarioRpt `json:"scenarios"`
	Summary     Summary       `json:"summary"`
}

type ScenarioRpt struct {
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Passed      bool              `json:"passed"`
	Assertions  []Assertion       `json:"assertions"`
	Requests    []RequestEvidence `json:"requests,omitempty"`
	Evidence    map[string]any    `json:"evidence,omitempty"`
	LogTail     string            `json:"server_log_tail,omitempty"`
	Error       string            `json:"error,omitempty"`
}

type Assertion struct {
	Name   string `json:"name"`
	Passed bool   `json:"passed"`
	Detail string `json:"detail,omitempty"`
}

type RequestEvidence struct {
	Label    string `json:"label"`
	Method   string `json:"method"`
	Path     string `json:"path"`
	Resp     Resp   `json:"response"`
	Key      string `json:"idempotency_key,omitempty"`
	Body     string `json:"request_body,omitempty"`
	Duration string `json:"duration,omitempty"`
}

type Summary struct {
	Total      int `json:"total"`
	Passed     int `json:"passed"`
	Failed     int `json:"failed"`
	Assertions int `json:"assertions"`
	AssertOK   int `json:"assertions_passed"`
}

// ---- 场景运行框架 ----

type runner struct {
	env  *Env
	http *http.Client
	sr   *ScenarioRpt
	reqs []RequestEvidence
}

func (r *runner) url(p string) string      { return r.env.BizURL() + p }
func (r *runner) auditURL(p string) string { return r.env.AuditURL() + p }

func (r *runner) check(name string, ok bool, detailFormat string, args ...any) {
	detail := ""
	if detailFormat != "" {
		detail = fmt.Sprintf(detailFormat, args...)
	}
	r.sr.Assertions = append(r.sr.Assertions, Assertion{Name: name, Passed: ok, Detail: detail})
}

func (r *runner) record(label, method, path, key string, body []byte, resp Resp, d time.Duration) {
	r.reqs = append(r.reqs, RequestEvidence{
		Label: label, Method: method, Path: path, Resp: resp,
		Key: key, Body: string(body), Duration: d.Round(time.Millisecond).String(),
	})
}

func (r *runner) deposit(label, key string, body map[string]any, headers map[string]string) Resp {
	payload, _ := json.Marshal(body)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	start := time.Now()
	resp := doRequest(ctx, r.http, http.MethodPost, r.url("/v1/deposits"), withKey(key, headers), payload)
	r.record(label, http.MethodPost, "/v1/deposits", key, payload, resp, time.Since(start))
	return resp
}

func (r *runner) get(path string) (int, map[string]any) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, r.url(path), nil)
	res, err := r.http.Do(req)
	if err != nil {
		return 0, map[string]any{"_error": err.Error()}
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	if m == nil {
		m = map[string]any{}
	}
	return res.StatusCode, m
}

func (r *runner) post(path string, v any) {
	body, _ := json.Marshal(v)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.url(path), bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.http.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
}

func (r *runner) reset() {
	r.post("/admin/reset", map[string]string{})
}

func (r *runner) advanceClock(ms int) {
	r.post("/admin/clock/advance", map[string]int{"ms": ms})
}

func (r *runner) auditFaults(body map[string]any) {
	payload, _ := json.Marshal(body)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, r.auditURL("/audit/faults"), bytes.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.http.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
}

func (r *runner) auditInspect() map[string]any {
	ctx, c := context.WithTimeout(context.Background(), 5*time.Second)
	defer c()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, r.auditURL("/audit/inspect"), nil)
	resp, err := r.http.Do(req)
	if err != nil {
		return map[string]any{"_error": err.Error()}
	}
	defer resp.Body.Close()
	var m map[string]any
	b, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(b, &m)
	if m == nil {
		m = map[string]any{}
	}
	return m
}

type ledgerView struct {
	Effects []map[string]any `json:"-"`
	Raw     map[string]any
}

func (r *runner) ledger() ledgerView {
	_, m := r.get("/admin/ledger")
	effects := []map[string]any{}
	if raw, ok := m["effects"].([]any); ok {
		for _, e := range raw {
			if mm, ok := e.(map[string]any); ok {
				effects = append(effects, mm)
			}
		}
	}
	return ledgerView{Effects: effects, Raw: m}
}

func withKey(key string, extra map[string]string) map[string]string {
	h := map[string]string{"Idempotency-Key": key}
	for k, v := range extra {
		h[k] = v
	}
	return h
}

func depositBody(account string, amount int64) map[string]any {
	return map[string]any{"account": account, "amount": amount, "memo": "acceptance"}
}

func txID(resp Resp) string {
	var m map[string]any
	if err := json.Unmarshal([]byte(resp.Body), &m); err != nil {
		return ""
	}
	if v, ok := m["tx_id"].(string); ok {
		return v
	}
	return ""
}

func replayHeader(resp Resp) string { return resp.Headers["Idempotency-Status"] }

// waitForFinal 对同键同正文持续重试，直到拿到 200（created/replayed）或超出预算。
func (r *runner) waitForFinal(label, key string, body map[string]any, extra map[string]string) Resp {
	var resp Resp
	for i := 0; i < 40; i++ {
		resp = r.deposit(fmt.Sprintf("%s#%d", label, i+1), key, body, extra)
		if resp.StatusCode == http.StatusOK {
			return resp
		}
		if resp.StatusCode != http.StatusAccepted {
			return resp // 409/5xx/断线：交给断言判定
		}
		time.Sleep(150 * time.Millisecond)
	}
	return resp
}

// ---- 顶层编排 ----

func runAcceptance(workDir string, ttl, waitMax time.Duration) Report {
	started := time.Now().UTC()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()

	env, err := NewEnv(ctx, workDir, ttl, waitMax)
	if err != nil {
		return Report{
			GeneratedAt: started.Format(time.RFC3339Nano), GoVersion: runtime.Version(),
			TTL: ttl.String(), WaitMax: waitMax.String(), WorkDir: workDir,
			Scenarios: []ScenarioRpt{{Name: "harness", Passed: false, Error: err.Error()}},
			Summary:   Summary{Total: 1, Failed: 1},
		}
	}
	if err := env.Start(ctx); err != nil {
		return Report{
			GeneratedAt: started.Format(time.RFC3339Nano), GoVersion: runtime.Version(),
			TTL: ttl.String(), WaitMax: waitMax.String(), WorkDir: workDir,
			Scenarios: []ScenarioRpt{{
				Name: "harness", Passed: false, Error: err.Error(),
				LogTail: env.BizLogTail(),
			}},
			Summary: Summary{Total: 1, Failed: 1},
		}
	}
	defer env.Stop()

	type scenario struct {
		name, desc string
		fn         func(r *runner)
	}
	scenarios := []scenario{
		{"replay_basic", "同键同正文第二次请求原样重放，本地副作用仅一次",
			scenarioReplay},
		{"conflict_different_body", "同键不同正文返回 409 冲突，且不产生副作用",
			scenarioConflict},
		{"in_progress_concurrent", "处理中的重复请求先得到 202，随后拿到明确重放结果",
			scenarioInProgress},
		{"concurrent_retries_once", fmt.Sprintf("%d 个并发同键请求最终得到同一交易号，账本仅一笔", concurrencyN),
			scenarioConcurrent},
		{"disconnect_before_commit", "提交前断线：占位遗留；TTL 后同键重试成功，本地副作用仅一次（外部调用可重复）",
			scenarioDisconnectBefore},
		{"disconnect_after_commit", "提交后断线：事务已落盘，重试只重放，账本与外部调用都仅一次",
			scenarioDisconnectAfter},
		{"crash_recovery", "提交前进程崩溃，重启后占位仍在；TTL 后重试成功，账本仅一笔",
			scenarioCrash},
		{"replay_survives_restart", "成功提交后进程崩溃重启，结果与副作用都从 WAL 恢复并重放",
			scenarioReplayAfterRestart},
		{"external_not_exactly_once", "外部系统先落账再返回失败：重试导致外部两条事件，但本地账本仍仅一笔（明确不保证外部恰好一次）",
			scenarioExternalDuplicate},
	}

	rep := Report{
		GeneratedAt: started.Format(time.RFC3339Nano), GoVersion: runtime.Version(),
		TTL: ttl.String(), WaitMax: waitMax.String(), WorkDir: workDir,
	}
	for _, sc := range scenarios {
		sr := ScenarioRpt{Name: sc.name, Description: sc.desc, Evidence: map[string]any{}}
		r := &runner{env: env, http: &http.Client{Timeout: 20 * time.Second}, sr: &sr}
		r.reset()
		func() {
			defer func() {
				if rec := recover(); rec != nil {
					sr.Error = fmt.Sprintf("panic: %v", rec)
				}
				sr.Requests = r.reqs
				allPassed := sr.Error == ""
				for _, a := range sr.Assertions {
					if !a.Passed {
						allPassed = false
					}
				}
				if len(sr.Assertions) == 0 {
					allPassed = false
				}
				sr.Passed = allPassed
				if !allPassed {
					sr.LogTail = env.BizLogTail()
				}
			}()
			sc.fn(r)
		}()
		rep.Scenarios = append(rep.Scenarios, sr)
	}

	for _, s := range rep.Scenarios {
		rep.Summary.Total++
		if s.Passed {
			rep.Summary.Passed++
		} else {
			rep.Summary.Failed++
		}
		rep.Summary.Assertions += len(s.Assertions)
		for _, a := range s.Assertions {
			if a.Passed {
				rep.Summary.AssertOK++
			}
		}
	}
	return rep
}

const concurrencyN = 24
