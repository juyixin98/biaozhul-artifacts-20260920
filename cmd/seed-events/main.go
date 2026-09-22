// seed-events 是本地演示工具：登录 API 后批量推送样例事件。
//
//	# 推送 samples/events.sample.json
//	go run ./cmd/seed-events file samples/events.sample.json
//
//	# 生成 30 天基线 + 当日突增（用于统计异常 z>2.5 演示）
//	go run ./cmd/seed-events gen --employee-email bob.li@example.com --baseline 3 --today 20
//
// 全部数据仅发往本地 API，不访问任何外部服务。
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"os"
	"time"
)

type loginResp struct {
	AccessToken string `json:"access_token"`
}

type sampleEvent struct {
	EventID       string         `json:"event_id"`
	EventType     string         `json:"event_type"`
	EmployeeEmail string         `json:"employee_email"`
	OccurredAt    string         `json:"occurred_at"`
	Metadata      map[string]any `json:"metadata"`
}

type sampleFile struct {
	Events []sampleEvent `json:"events"`
}

type ingestEvent struct {
	EventID    string         `json:"event_id"`
	EventType  string         `json:"event_type"`
	EmployeeID string         `json:"employee_id"`
	OccurredAt string         `json:"occurred_at"`
	Metadata   map[string]any `json:"metadata,omitempty"`
}

type employee struct {
	ID       string `json:"id"`
	Email    string `json:"email"`
	Name     string `json:"name"`
	Timezone string `json:"timezone"`
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	url := envOr("API_URL", "http://127.0.0.1:8080")
	username := envOr("API_USER", "admin")
	password := envOr("API_PASSWORD", "admin12345")

	token, err := login(url, username, password)
	if err != nil {
		fatal("login: %v", err)
	}
	empByEmail, err := loadEmployees(url, token)
	if err != nil {
		fatal("load employees: %v", err)
	}

	switch os.Args[1] {
	case "file":
		fs := flag.NewFlagSet("file", flag.ExitOnError)
		path := fs.String("file", "", "sample JSON file path")
		_ = fs.Parse(os.Args[2:])
		args := fs.Args()
		if *path == "" {
			if len(args) == 0 {
				fatal("file: --file is required")
			}
			*path = args[0]
		}
		runFile(url, token, *path, empByEmail)
	case "gen":
		fs := flag.NewFlagSet("gen", flag.ExitOnError)
		mode := fs.String("mode", "zscore", "zscore | burst")
		email := fs.String("employee-email", "", "target employee email")
		baseline := fs.Int("baseline", 3, "downloads per historical day (zscore)")
		today := fs.Int("today", 20, "downloads today (zscore)")
		burstN := fs.Int("count", 51, "downloads in the 10 minute window (burst)")
		days := fs.Int("days", 29, "history days before today (zscore)")
		ago := fs.Duration("ago", 30*time.Minute, "offset of the burst window before now (burst)")
		_ = fs.Parse(os.Args[2:])
		if *email == "" {
			fatal("gen: --employee-email is required")
		}
		switch *mode {
		case "zscore":
			runGen(url, token, empByEmail, *email, *baseline, *today, *days)
		case "burst":
			runBurst(url, token, empByEmail, *email, *burstN, *ago)
		default:
			fatal("unknown mode %q", *mode)
		}
	case "help", "-h", "--help":
		usage()
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  seed-events file --file samples/events.sample.json
  seed-events gen  --employee-email bob.li@example.com [--baseline 3] [--today 20] [--days 29]

env: API_URL (default http://127.0.0.1:8080), API_USER (admin), API_PASSWORD (admin12345)`)
}

func login(base, user, pass string) (string, error) {
	body, _ := json.Marshal(map[string]string{"username": user, "password": pass})
	resp, err := http.Post(base+"/api/v1/auth/login", "application/json", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("status %d: %s", resp.StatusCode, raw)
	}
	var lr loginResp
	if err := json.Unmarshal(raw, &lr); err != nil {
		return "", err
	}
	if lr.AccessToken == "" {
		return "", fmt.Errorf("empty token: %s", raw)
	}
	return lr.AccessToken, nil
}

func loadEmployees(base, token string) (map[string]employee, error) {
	req, _ := http.NewRequest(http.MethodGet, base+"/api/v1/employees", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out struct {
		Items []employee `json:"items"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	m := make(map[string]employee, len(out.Items))
	for _, e := range out.Items {
		m[e.Email] = e
	}
	return m, nil
}

func runFile(base, token, path string, emps map[string]employee) {
	raw, err := os.ReadFile(path)
	if err != nil {
		fatal("read %s: %v", path, err)
	}
	var sf sampleFile
	if err := json.Unmarshal(raw, &sf); err != nil {
		fatal("parse sample file: %v", err)
	}

	now := time.Now().UTC()
	evs := make([]ingestEvent, 0, len(sf.Events))
	for i, se := range sf.Events {
		emp, ok := emps[se.EmployeeEmail]
		if !ok {
			fatal("sample event %d: unknown employee email %q", i, se.EmployeeEmail)
		}
		ts, err := resolveTime(se.OccurredAt, emp.Timezone, now)
		if err != nil {
			fatal("sample event %d (%s): %v", i, se.EventID, err)
		}
		evs = append(evs, ingestEvent{
			EventID:    se.EventID,
			EventType:  se.EventType,
			EmployeeID: emp.ID,
			OccurredAt: ts.UTC().Format(time.RFC3339Nano),
			Metadata:   se.Metadata,
		})
	}
	sendBatches(base, token, evs, 500)
	fmt.Printf("done: %d sample events from %s\n", len(evs), path)
}

// resolveTime 支持三种时间写法：
//  1. RFC3339（如 2026-09-20T13:00:00Z）
//  2. now-15m / now-2h（相对当前的 Go duration）
//  3. local:2026-09-19T21:30:00 —— 按员工时区解释的本地时间
func resolveTime(s, tzName string, now time.Time) (time.Time, error) {
	if len(s) > 4 && s[:4] == "now-" {
		d, err := time.ParseDuration(s[4:])
		if err != nil {
			return time.Time{}, err
		}
		return now.Add(-d), nil
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, nil
	}
	if len(s) > 6 && s[:6] == "local:" {
		loc, err := time.LoadLocation(tzName)
		if err != nil {
			return time.Time{}, err
		}
		t, err := time.ParseInLocation("2006-01-02T15:04:05", s[6:], loc)
		if err != nil {
			return time.Time{}, err
		}
		return t, nil
	}
	return time.Time{}, fmt.Errorf("unrecognized occurred_at %q (use RFC3339, now-15m or local:YYYY-MM-DDTHH:MM:SS)", s)
}

func runGen(base, token string, emps map[string]employee, email string, baseline, today, days int) {
	emp, ok := emps[email]
	if !ok {
		fatal("unknown employee %q", email)
	}
	loc, err := time.LoadLocation(emp.Timezone)
	if err != nil {
		fatal("bad timezone %q: %v", emp.Timezone, err)
	}
	rng := rand.New(rand.NewSource(20260920))
	now := time.Now().UTC()
	evs := make([]ingestEvent, 0, days*baseline+today)
	counter := 0
	add := func(t time.Time, typ string, meta map[string]any) {
		counter++
		evs = append(evs, ingestEvent{
			EventID:    fmt.Sprintf("gen-%s-%06d", emp.ID[:8], counter),
			EventType:  typ,
			EmployeeID: emp.ID,
			OccurredAt: t.UTC().Format(time.RFC3339Nano),
			Metadata:   meta,
		})
	}

	// 历史：过去 days 天（不含今天），每天 baseline 个工作时段下载（09:00-18:00 本地）。
	localNow := now.In(loc)
	today0 := time.Date(localNow.Year(), localNow.Month(), localNow.Day(), 0, 0, 0, 0, loc)
	for d := days; d >= 1; d-- {
		day := today0.AddDate(0, 0, -d)
		for i := 0; i < baseline; i++ {
			hour := 9 + (i * 9 / (baseline + 1)) + rng.Intn(2)
			minute := rng.Intn(60)
			t := day.Add(time.Duration(hour)*time.Hour + time.Duration(minute)*time.Minute)
			add(t, "file_download", map[string]any{
				"file": fmt.Sprintf("report_d%02d_%d.pdf", d, i), "size_bytes": 10000 + rng.Intn(500000),
			})
		}
	}
	// 当日突增：今天上午 9:00 起均匀分布的 today 个下载。
	for i := 0; i < today; i++ {
		t := today0.Add(9*time.Hour + time.Duration(i*2)*time.Minute)
		if t.After(localNow) {
			// 不生成未来时间；改为使用“当前之前”的紧凑序列。
			t = localNow.Add(-time.Duration(today-i) * time.Minute)
		}
		add(t, "file_download", map[string]any{
			"file": fmt.Sprintf("bulk_%03d.zip", i), "size_bytes": 200000 + rng.Intn(800000),
		})
	}
	sendBatches(base, token, evs, 500)
	fmt.Printf("done: generated %d events for %s (%d history days x %d + %d today)\n",
		len(evs), email, days, baseline, today)
}

func runBurst(base, token string, emps map[string]employee, email string, n int, ago time.Duration) {
	emp, ok := emps[email]
	if !ok {
		fatal("unknown employee %q", email)
	}
	if n < 51 {
		fatal("burst: --count must be >= 51 to cross the threshold")
	}
	// 落在同一个 10 分钟对齐桶：以 (now-ago) 的桶起点 +1 分钟为首个事件。
	now := time.Now().UTC()
	win := 10 * time.Minute
	bucketStart := now.Add(-ago).Truncate(win)
	first := bucketStart.Add(time.Minute)
	if first.Add(time.Duration(n-1) * 5 * time.Second).After(now) {
		// 桶内容纳不下（离现在太近），就把事件紧凑排到 now 之前。
		first = now.Add(-time.Duration(n) * 5 * time.Second).Truncate(time.Second)
	}
	evs := make([]ingestEvent, 0, n)
	for i := 0; i < n; i++ {
		t := first.Add(time.Duration(i) * 5 * time.Second)
		evs = append(evs, ingestEvent{
			EventID:    fmt.Sprintf("burst-%s-%06d", emp.ID[:8], i+1),
			EventType:  "file_download",
			EmployeeID: emp.ID,
			OccurredAt: t.UTC().Format(time.RFC3339Nano),
			Metadata: map[string]any{
				"file": fmt.Sprintf("dump_%03d.dat", i), "size_bytes": 4096,
			},
		})
	}
	sendBatches(base, token, evs, 2000)
	fmt.Printf("done: burst %d downloads for %s in bucket starting %s UTC\n",
		n, email, first.Truncate(win).Format(time.RFC3339))
}

func sendBatches(base, token string, evs []ingestEvent, size int) {
	client := &http.Client{Timeout: 60 * time.Second}
	var accepted, duplicated int
	for start := 0; start < len(evs); start += size {
		end := start + size
		if end > len(evs) {
			end = len(evs)
		}
		body, _ := json.Marshal(map[string]any{"events": evs[start:end]})
		req, _ := http.NewRequest(http.MethodPost, base+"/api/v1/events/batch", bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			fatal("post batch: %v", err)
		}
		raw, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode >= 300 {
			fatal("batch rejected status=%d body=%s", resp.StatusCode, truncate(string(raw), 800))
		}
		var r struct {
			AcceptedCount  int `json:"accepted_count"`
			DuplicateCount int `json:"duplicate_count"`
		}
		_ = json.Unmarshal(raw, &r)
		accepted += r.AcceptedCount
		duplicated += r.DuplicateCount
	}
	fmt.Printf("posted=%d accepted=%d duplicate=%d\n", len(evs), accepted, duplicated)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "seed-events: "+format+"\n", args...)
	os.Exit(1)
}
