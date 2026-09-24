package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"tztrigger/internal/engine"
)

type fakeClock struct{ t time.Time }

func (f *fakeClock) Now() time.Time { return f.t }

func newTestServer(t *testing.T) (*httptest.Server, *engine.Engine) {
	t.Helper()
	fc := &fakeClock{t: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)}
	eng := engine.New(fc, time.Second)
	srv := httptest.NewServer(New(eng))
	t.Cleanup(srv.Close)
	return srv, eng
}

func post(t *testing.T, url, body string) *http.Response {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	return resp
}

func get(t *testing.T, url string) *http.Response {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	return resp
}

func readJSON(t *testing.T, resp *http.Response) map[string]any {
	t.Helper()
	defer resp.Body.Close()
	var buf bytes.Buffer
	_, _ = buf.ReadFrom(resp.Body)
	var v map[string]any
	if err := json.Unmarshal(buf.Bytes(), &v); err != nil {
		t.Fatalf("响应不是合法 JSON: %v\n%s", err, buf.String())
	}
	return v
}

func TestHealthzAndVersion(t *testing.T) {
	srv, _ := newTestServer(t)

	resp := get(t, srv.URL+"/healthz")
	if resp.StatusCode != 200 {
		t.Fatalf("healthz = %d", resp.StatusCode)
	}
	resp.Body.Close()

	resp = get(t, srv.URL+"/version")
	v := readJSON(t, resp)
	if v["tzdata_version"] != "2024a" {
		t.Errorf("tzdata_version = %v，期望 2024a", v["tzdata_version"])
	}
	if v["zoneinfo_sha256"] == "" || v["go_version"] == "" {
		t.Errorf("version 响应缺字段: %v", v)
	}
}

func TestTriggerLifecycle(t *testing.T) {
	srv, eng := newTestServer(t)

	// 创建
	resp := post(t, srv.URL+"/triggers", `{
		"id": "weekday-morning",
		"minutes": "30", "hours": "9", "weekdays": "1-5",
		"timezone": "America/New_York", "catch_up_limit": 10
	}`)
	if resp.StatusCode != 201 {
		b := readJSON(t, resp)
		t.Fatalf("创建 = %d: %v", resp.StatusCode, b)
	}
	created := readJSON(t, resp)
	if created["id"] != "weekday-morning" {
		t.Errorf("id = %v", created["id"])
	}
	nf, ok := created["next_fire"].(map[string]any)
	if !ok || nf["logical_id"] == "" {
		t.Errorf("缺少 next_fire: %v", created)
	}

	// 重复 ID → 409
	resp = post(t, srv.URL+"/triggers", `{
		"id": "weekday-morning",
		"minutes": "0", "hours": "9", "weekdays": "1-5", "timezone": "UTC"
	}`)
	if resp.StatusCode != 409 {
		t.Fatalf("重复 ID = %d，期望 409", resp.StatusCode)
	}
	resp.Body.Close()

	// 列表
	resp = get(t, srv.URL+"/triggers")
	list := readJSON(t, resp)
	if trigs, _ := list["triggers"].([]any); len(trigs) != 1 {
		t.Errorf("列表长度 = %v，期望 1", len(trigs))
	}

	// 详情
	resp = get(t, srv.URL+"/triggers/weekday-morning")
	if resp.StatusCode != 200 {
		t.Fatalf("详情 = %d", resp.StatusCode)
	}
	readJSON(t, resp)

	// 预览（秋季切换窗口，应只有 3 次且 11-01 取较早一次）
	resp = get(t, srv.URL+"/triggers/weekday-morning/preview"+
		"?from=2026-10-30T00:00:00Z&to=2026-11-03T00:00:00Z")
	pv := readJSON(t, resp)
	fires, _ := pv["fires"].([]any)
	// 09:30 不在 DST 切换点（01:00-02:00），每天一次：10-30(五)、11-02(一)
	// 10-31(六)、11-01(日) 非工作日 → 2 次
	if len(fires) != 2 {
		t.Errorf("预览命中 %d 次，期望 2 次（周五+周一）: %v", len(fires), fires)
	}

	// 推进引擎产生事件后查询 /events
	eng.Advance(time.Date(2026, 9, 25, 14, 0, 0, 0, time.UTC)) // 跨过 09-25 09:30 EDT
	resp = get(t, srv.URL+"/events?trigger_id=weekday-morning")
	ev := readJSON(t, resp)
	events, _ := ev["events"].([]any)
	if len(events) == 0 {
		t.Error("推进后应有触发事件")
	}

	// 删除
	req, _ := http.NewRequest("DELETE", srv.URL+"/triggers/weekday-morning", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != 204 {
		t.Fatalf("删除 = %d，期望 204", resp.StatusCode)
	}
	resp.Body.Close()

	resp = get(t, srv.URL+"/triggers/weekday-morning")
	if resp.StatusCode != 404 {
		t.Fatalf("删除后详情 = %d，期望 404", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestCreateValidation(t *testing.T) {
	srv, _ := newTestServer(t)
	cases := []struct {
		name string
		body string
		want int
	}{
		{"缺字段", `{"minutes": "0"}`, 400},
		{"非法表达式", `{"minutes": "99", "hours": "9", "weekdays": "1", "timezone": "UTC"}`, 400},
		{"未知时区", `{"minutes": "0", "hours": "9", "weekdays": "1", "timezone": "Mars/Olympus"}`, 400},
		{"非法JSON", `{not json`, 400},
		{"未知字段", `{"minutes": "0", "hours": "9", "weekdays": "1", "timezone": "UTC", "foo": 1}`, 400},
		{"负上限", `{"minutes": "0", "hours": "9", "weekdays": "1", "timezone": "UTC", "catch_up_limit": -5}`, 400},
	}
	for _, c := range cases {
		resp := post(t, srv.URL+"/triggers", c.body)
		if resp.StatusCode != c.want {
			t.Errorf("%s: = %d，期望 %d", c.name, resp.StatusCode, c.want)
		}
		v := readJSON(t, resp)
		if v["error"] == "" {
			t.Errorf("%s: 错误响应应含 error 字段", c.name)
		}
	}
}

func TestAutoID(t *testing.T) {
	srv, _ := newTestServer(t)
	resp := post(t, srv.URL+"/triggers",
		`{"minutes": "0", "hours": "9", "weekdays": "*", "timezone": "Asia/Shanghai"}`)
	created := readJSON(t, resp)
	id, _ := created["id"].(string)
	if !strings.HasPrefix(id, "trig-") {
		t.Errorf("自动 ID = %q，期望 trig- 前缀", id)
	}
}
