// Command fixturetest 只执行 testdata/fixtures/commands.json 中显式列出的
// 本地命令，逐一比对退出码，并在最后对本机 HTTP 服务做一次冒烟验证。
//
// 该程序不联网、不执行任何未在夹具中列出的命令。
// 用法:
//
//	go run ./scripts/fixturetest            # 执行并把结果写入 reports/fixture-report.json
//	go run ./scripts/fixturetest -v         # 同时把每条命令输出回显到终端
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

type commandSpec struct {
	Name       string   `json:"name"`
	Argv       []string `json:"argv"`
	ExpectExit int      `json:"expect_exit"`
}

type manifest struct {
	Description string        `json:"description"`
	Commands    []commandSpec `json:"commands"`
}

type commandResult struct {
	Name       string `json:"name"`
	Argv       string `json:"argv"`
	ExpectExit int    `json:"expect_exit"`
	ActualExit int    `json:"actual_exit"`
	Pass       bool   `json:"pass"`
	DurationMS int64  `json:"duration_ms"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	Error      string `json:"error,omitempty"`
}

type httpCheck struct {
	Name       string `json:"name"`
	Pass       bool   `json:"pass"`
	StatusCode int    `json:"status_code"`
	Detail     string `json:"detail,omitempty"`
}

type report struct {
	StartedAt      string          `json:"started_at"`
	FinishedAt     string          `json:"finished_at"`
	RepoRoot       string          `json:"repo_root"`
	GoVersion      string          `json:"go_version"`
	CommandResults []commandResult `json:"command_results"`
	HTTPChecks     []httpCheck     `json:"http_checks"`
	AllPassed      bool            `json:"all_passed"`
}

func main() {
	verbose := flag.Bool("v", false, "回显命令输出")
	flag.Parse()

	root, err := repoRoot()
	must(err)

	manPath := filepath.Join(root, "testdata", "fixtures", "commands.json")
	raw, err := os.ReadFile(manPath)
	must(err)
	var man manifest
	must(json.Unmarshal(raw, &man))

	rep := report{
		StartedAt: time.Now().UTC().Format(time.RFC3339),
		RepoRoot:  root,
		GoVersion: goVersion(root),
		AllPassed: true,
	}

	for _, spec := range man.Commands {
		res := runCommand(root, spec)
		rep.CommandResults = append(rep.CommandResults, res)
		if !res.Pass {
			rep.AllPassed = false
		}
		if *verbose {
			fmt.Printf("[$ %s] pass=%v exit=%d (期望 %d)\n", res.Argv, res.Pass, res.ActualExit, res.ExpectExit)
			if res.Stdout != "" {
				fmt.Print(indent(res.Stdout))
			}
			if res.Stderr != "" {
				fmt.Print(indent(res.Stderr))
			}
		}
	}

	// HTTP 冒烟：只启动本机回环服务，请求两个端点后关闭。
	rep.HTTPChecks = append(rep.HTTPChecks, smokeHTTPServer(root)...)
	for _, hc := range rep.HTTPChecks {
		if !hc.Pass {
			rep.AllPassed = false
		}
		if *verbose {
			fmt.Printf("[http %s] pass=%v code=%d %s\n", hc.Name, hc.Pass, hc.StatusCode, hc.Detail)
		}
	}
	rep.FinishedAt = time.Now().UTC().Format(time.RFC3339)

	reportDir := filepath.Join(root, "reports")
	must(os.MkdirAll(reportDir, 0o755))
	outPath := filepath.Join(reportDir, "fixture-report.json")
	out, err := json.MarshalIndent(rep, "", "  ")
	must(err)
	must(os.WriteFile(outPath, out, 0o644))

	fmt.Printf("夹具验收完成: %d 条命令, %d 项 HTTP 检查, 全部通过=%v\n报告: %s\n",
		len(rep.CommandResults), len(rep.HTTPChecks), rep.AllPassed, outPath)
	if !rep.AllPassed {
		os.Exit(1)
	}
}

func runCommand(root string, spec commandSpec) commandResult {
	res := commandResult{
		Name:       spec.Name,
		Argv:       joinArgv(spec.Argv),
		ExpectExit: spec.ExpectExit,
	}
	if len(spec.Argv) == 0 {
		res.Error = "夹具命令 argv 为空"
		return res
	}
	start := time.Now()
	cmd := exec.Command(spec.Argv[0], spec.Argv[1:]...)
	cmd.Dir = root
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	res.DurationMS = time.Since(start).Milliseconds()
	res.Stdout = stdout.String()
	res.Stderr = stderr.String()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			res.ActualExit = ee.ExitCode()
		} else {
			res.Error = err.Error()
			res.Pass = false
			return res
		}
	} else {
		res.ActualExit = 0
	}
	res.Pass = res.ActualExit == spec.ExpectExit
	return res
}

// smokeHTTPServer 在独立子进程中启动 serve，健康检查与一次判定后关闭。
// 使用子进程而非 goroutine，确保端到端覆盖真实的监听/命令行路径。
// 监听端口由内核临时分配，避免与环境中已有服务冲突。
func smokeHTTPServer(root string) []httpCheck {
	addr, err := freePortAddr()
	if err != nil {
		return []httpCheck{{Name: "server_start", Pass: false, Detail: err.Error()}}
	}
	work := filepath.Join(root, ".smoke-work")
	cacheDir := filepath.Join(root, ".smoke-cache")
	_ = os.MkdirAll(work, 0o755)

	bin := filepath.Join(root, "bin", "licensejudge")
	cmd := exec.Command(bin, "serve",
		"--policy", filepath.Join(root, "configs", "policy.json"),
		"--addr", addr,
		"--work-dir", work,
		"--cache-dir", cacheDir)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return []httpCheck{{
			Name: "server_start", Pass: false, Detail: err.Error(),
		}}
	}
	defer func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
	}()

	base := "http://" + addr
	var checks []httpCheck
	checks = append(checks, waitForHealth(base))
	checks = append(checks, postEvaluate(base, `{"expression":"(MIT OR GPL-3.0-only) AND Apache-2.0"}`, "allow"))
	checks = append(checks, postEvaluate(base, `{"expression":"Unknown-Smoke-X"}`, "unknown"))
	if !checks[0].Pass && stderr.Len() > 0 {
		checks[0].Detail += "; server stderr: " + stderr.String()
	}
	return checks
}

// freePortAddr 通过"监听 :0 后立即关闭"的方式取得一个内核临时分配的端口。
func freePortAddr() (string, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr, nil
}

func waitForHealth(base string) httpCheck {
	deadline := time.Now().Add(5 * time.Second)
	client := &http.Client{Timeout: time.Second}
	var last string
	for time.Now().Before(deadline) {
		resp, err := client.Get(base + "/healthz")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			ok := resp.StatusCode == 200 && bytes.Contains(body, []byte(`"ok"`))
			return httpCheck{Name: "healthz", Pass: ok, StatusCode: resp.StatusCode, Detail: string(body)}
		}
		last = err.Error()
		time.Sleep(100 * time.Millisecond)
	}
	return httpCheck{Name: "healthz", Pass: false, Detail: "等待服务就绪超时: " + last}
}

func postEvaluate(base, body, wantVerdict string) httpCheck {
	resp, err := http.Post(base+"/v1/evaluate", "application/json", bytes.NewReader([]byte(body)))
	if err != nil {
		return httpCheck{Name: "evaluate:" + wantVerdict, Pass: false, Detail: err.Error()}
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	var parsed struct {
		Result struct {
			Verdict string `json:"verdict"`
		} `json:"result"`
		Error string `json:"error"`
	}
	_ = json.Unmarshal(respBody, &parsed)
	pass := resp.StatusCode == 200 && parsed.Result.Verdict == wantVerdict
	return httpCheck{
		Name:       "evaluate:" + wantVerdict,
		Pass:       pass,
		StatusCode: resp.StatusCode,
		Detail:     string(respBody),
	}
}

func goVersion(root string) string {
	out, err := exec.Command("go", "version").Output()
	if err != nil {
		return ""
	}
	return string(bytes.TrimSpace(out))
}

func repoRoot() (string, error) {
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	if filepath.Base(wd) == "scripts" {
		return filepath.Dir(wd), nil
	}
	return wd, nil
}

func joinArgv(argv []string) string {
	var b bytes.Buffer
	for i, a := range argv {
		if i > 0 {
			b.WriteByte(' ')
		}
		if needsQuote(a) {
			fmt.Fprintf(&b, "%q", a)
		} else {
			b.WriteString(a)
		}
	}
	return b.String()
}

func needsQuote(s string) bool {
	if s == "" {
		return true
	}
	for _, r := range s {
		if r == ' ' || r == '\t' || r == '"' {
			return true
		}
	}
	return false
}

func indent(s string) string {
	var b bytes.Buffer
	for _, line := range bytes.Split([]byte(s), []byte{'\n'}) {
		if len(line) > 0 {
			b.WriteString("    ")
			b.Write(line)
			b.WriteByte('\n')
		}
	}
	return b.String()
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(2)
	}
}
