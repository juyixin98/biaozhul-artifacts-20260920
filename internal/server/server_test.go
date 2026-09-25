package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"resourcebooking/internal/clock"
	"resourcebooking/internal/scheduler"
)

var testEpoch = time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC)

func newTestServer(t *testing.T) (*Server, *httptest.Server, *clock.Fake) {
	t.Helper()
	fc := clock.NewFake(testEpoch)
	sch := scheduler.New(scheduler.NewEventLog(fc))
	srv := New(sch, Config{Clock: fc, Epoch: testEpoch, DispatchEvery: time.Hour})
	hs := httptest.NewServer(srv.Handler())
	t.Cleanup(hs.Close)
	return srv, hs, fc
}

func doJSON(t *testing.T, hs *httptest.Server, method, path string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(data)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, hs.URL+path, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("响应不是 JSON（状态 %d）: %v", resp.StatusCode, err)
	}
	return resp.StatusCode, out
}

func TestHTTPEndToEnd(t *testing.T) {
	_, hs, _ := newTestServer(t)

	// 建资源：两维容量 [1,0]（第二维为 0）。
	status, body := doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "room", "name": "会议室", "capacity": []int64{1, 0},
	})
	if status != http.StatusCreated {
		t.Fatalf("建资源状态 %d: %v", status, body)
	}

	// 预约 09:00-10:00，需求 [1,0]。
	status, body = doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "meet-a", "resource": "room",
		"interval": map[string]any{
			"start": "2030-01-01T09:00:00Z",
			"end":   "2030-01-01T10:00:00Z",
		},
		"demand": []int64{1, 0},
	})
	if status != http.StatusCreated {
		t.Fatalf("预约状态 %d: %v", status, body)
	}

	// 相邻区间 10:00-11:00 必须成功。
	status, body = doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "meet-b", "resource": "room",
		"interval": map[string]any{
			"start": "2030-01-01T10:00:00Z",
			"end":   "2030-01-01T11:00:00Z",
		},
		"demand": []int64{1, 0},
	})
	if status != http.StatusCreated {
		t.Fatalf("相邻预约应成功，状态 %d: %v", status, body)
	}

	// 重叠区间必须 409。
	status, body = doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"resource": "room",
		"interval": map[string]any{
			"start": "2030-01-01T09:30:00Z",
			"end":   "2030-01-01T10:30:00Z",
		},
		"demand": []int64{1, 0},
	})
	if status != http.StatusConflict || body["code"] != scheduler.CodeConflict {
		t.Fatalf("重叠预约应 409/conflict，得到 %d %v", status, body)
	}

	// 零容量维度上的正需求应 400。
	status, body = doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"resource": "room",
		"interval": map[string]any{
			"start": "2030-01-01T08:00:00Z",
			"end":   "2030-01-01T08:30:00Z",
		},
		"demand": []int64{0, 1},
	})
	if status != http.StatusBadRequest {
		t.Fatalf("零容量正需求应 400，得到 %d %v", status, body)
	}

	// 最早可行位置：窗口 08:00-13:00、时长 2h。
	// [09,10) 与 [10,11) 已占满容量 1，最早 2h 空位为 [11,13)。
	status, body = doJSON(t, hs, "POST", "/v1/reservations:earliest-feasible", map[string]any{
		"resource": "room",
		"window": map[string]any{
			"start": "2030-01-01T08:00:00Z",
			"end":   "2030-01-01T13:00:00Z",
		},
		"duration_minutes": 120,
		"demand":           []int64{1, 0},
	})
	if status != http.StatusOK || body["feasible"] != true {
		t.Fatalf("最早可行查询异常: %d %v", status, body)
	}
	iv := body["interval"].(map[string]any)
	if iv["start"] != "2030-01-01T11:00:00Z" || iv["end"] != "2030-01-01T13:00:00Z" {
		t.Fatalf("期望 [11:00,13:00)，得到 %v", iv)
	}

	// 窗口 08:00-12:00 只剩 [11,12) 一格空，放不下 2h：不可行但不是错误。
	status, body = doJSON(t, hs, "POST", "/v1/reservations:earliest-feasible", map[string]any{
		"resource": "room",
		"window": map[string]any{
			"start": "2030-01-01T08:00:00Z",
			"end":   "2030-01-01T12:00:00Z",
		},
		"duration_minutes": 120,
		"demand":           []int64{1, 0},
	})
	if status != http.StatusOK || body["feasible"] != false {
		t.Fatalf("期望 feasible=false，得到 %d %v", status, body)
	}
}

func TestHTTPBatchPartialConflictRollback(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{1},
	})
	// 预置占用。
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "busy", "resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T10:00:00Z",
		},
		"demand": []int64{1},
	})
	// 批次：一条冲突 + 一条相邻可行。
	status, body := doJSON(t, hs, "POST", "/v1/reservations:batch", map[string]any{
		"items": []map[string]any{{
			"fixed": map[string]any{
				"id": "good", "resource": "r",
				"interval": map[string]any{
					"start": "2030-01-01T10:00:00Z",
					"end":   "2030-01-01T11:00:00Z",
				},
				"demand": []int64{1},
			},
		}, {
			"fixed": map[string]any{
				"id": "bad", "resource": "r",
				"interval": map[string]any{
					"start": "2030-01-01T01:00:00Z",
					"end":   "2030-01-01T02:00:00Z",
				},
				"demand": []int64{1},
			},
		}},
	})
	if status != http.StatusConflict {
		t.Fatalf("期望 409，得到 %d %v", status, body)
	}
	details := body["details"].([]any)
	if len(details) != 1 {
		t.Fatalf("期望 1 个失败条目，得到 %v", details)
	}
	// good 不得落地。
	status, body = doJSON(t, hs, "GET", "/v1/reservations/good", nil)
	if status != http.StatusNotFound {
		t.Fatalf("回滚后 good 应不存在，得到 %d %v", status, body)
	}
}

func TestHTTPBatchAutoPlace(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{2},
	})
	status, body := doJSON(t, hs, "POST", "/v1/reservations:batch", map[string]any{
		"items": []map[string]any{{
			"place": map[string]any{
				"id": "p1", "resource": "r",
				"window": map[string]any{
					"start": "2030-01-01T00:00:00Z",
					"end":   "2030-01-01T05:00:00Z",
				},
				"duration_minutes": 120,
				"demand":           []int64{1},
			},
		}, {
			"place": map[string]any{
				"id": "p2", "resource": "r",
				"window": map[string]any{
					"start": "2030-01-01T00:00:00Z",
					"end":   "2030-01-01T05:00:00Z",
				},
				"duration_minutes": 120,
				"demand":           []int64{1},
			},
		}},
	})
	if status != http.StatusCreated {
		t.Fatalf("自动放置批次应成功: %d %v", status, body)
	}
	rs := body["reservations"].([]any)
	if len(rs) != 2 {
		t.Fatalf("期望 2 条，得到 %v", rs)
	}
	// 容量 2，两笔应都落在窗口起点。
	for i, r := range rs {
		iv := r.(map[string]any)["interval"].(map[string]any)
		if iv["start"] != "2030-01-01T00:00:00Z" {
			t.Fatalf("条目 %d 期望 00:00 起点，得到 %v", i, iv)
		}
	}
}

func TestHTTPClockAdvanceAndEvents(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "job", "resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T01:00:00Z",
			"end":   "2030-01-01T02:00:00Z",
		},
		"demand": []int64{1},
	})

	// 推进到 01:00 -> running。
	status, body := doJSON(t, hs, "POST", "/test/clock/advance", map[string]any{
		"to": "2030-01-01T01:00:00Z",
	})
	if status != http.StatusOK {
		t.Fatalf("推进失败 %d %v", status, body)
	}
	started := body["started"].([]any)
	if len(started) != 1 || started[0] != "job" {
		t.Fatalf("期望 job 开始，得到 %v", started)
	}

	// 推进到 02:00 -> completed。
	_, body = doJSON(t, hs, "POST", "/test/clock/advance", map[string]any{
		"to": "2030-01-01T02:00:00Z",
	})
	completed := body["completed"].([]any)
	if len(completed) != 1 || completed[0] != "job" {
		t.Fatalf("期望 job 完成，得到 %v", completed)
	}

	// 事件流应包含 created/started/completed。
	status, body = doJSON(t, hs, "GET", "/v1/events", nil)
	if status != http.StatusOK {
		t.Fatalf("事件查询失败 %d", status)
	}
	evs := body["events"].([]any)
	var types []string
	for _, e := range evs {
		types = append(types, e.(map[string]any)["type"].(string))
	}
	joined := strings.Join(types, ",")
	for _, want := range []string{"reservation.created", "reservation.started", "reservation.completed"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("事件流缺少 %s: %v", want, types)
		}
	}
}

func TestHTTPCancel(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "x", "resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T05:00:00Z",
		},
		"demand": []int64{1},
	})
	status, body := doJSON(t, hs, "DELETE", "/v1/reservations/x", nil)
	if status != http.StatusOK || body["status"] != "cancelled" {
		t.Fatalf("取消失败: %d %v", status, body)
	}
	// 取消后同一区间可约。
	status, _ = doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "y", "resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T05:00:00Z",
		},
		"demand": []int64{1},
	})
	if status != http.StatusCreated {
		t.Fatalf("取消后容量未释放，状态 %d", status)
	}
}

func TestHTTPValidation(t *testing.T) {
	_, hs, _ := newTestServer(t)
	// 未知字段应 400。
	req, _ := http.NewRequest("POST", hs.URL+"/v1/resources",
		strings.NewReader(`{"id":"r","capacity":[1],"bogus":true}`))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("未知字段应 400，得到 %d", resp.StatusCode)
	}

	// 未对齐分钟的时间应 400。
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{1},
	})
	status, body := doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:30Z",
			"end":   "2030-01-01T01:00:00Z",
		},
		"demand": []int64{1},
	})
	if status != http.StatusBadRequest {
		t.Fatalf("非分钟对齐时间应 400，得到 %d %v", status, body)
	}
}

func TestHealthz(t *testing.T) {
	_, hs, _ := newTestServer(t)
	resp, err := http.Get(hs.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz 状态 %d", resp.StatusCode)
	}
}

func TestHTTPListFilterAndNotFound(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r1", "capacity": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r2", "capacity": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "a", "resource": "r1",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T01:00:00Z",
		},
		"demand": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "b", "resource": "r2",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T01:00:00Z",
		},
		"demand": []int64{1},
	})

	status, body := doJSON(t, hs, "GET", "/v1/resources", nil)
	if status != http.StatusOK {
		t.Fatalf("list resources %d", status)
	}
	if len(body["resources"].([]any)) != 2 {
		t.Fatal("应列出 2 个资源")
	}
	status, body = doJSON(t, hs, "GET", "/v1/reservations?resource=r1", nil)
	if len(body["reservations"].([]any)) != 1 {
		t.Fatalf("资源过滤应剩 1 条，得到 %v", body)
	}
	status, body = doJSON(t, hs, "GET", "/v1/reservations/nope", nil)
	if status != http.StatusNotFound {
		t.Fatalf("未知预约应 404，得到 %d", status)
	}
	status, _ = doJSON(t, hs, "GET", "/v1/resources/nope", nil)
	if status != http.StatusNotFound {
		t.Fatalf("未知资源应 404，得到 %d", status)
	}
}

func TestHTTPEventsAfterSeq(t *testing.T) {
	_, hs, _ := newTestServer(t)
	doJSON(t, hs, "POST", "/v1/resources", map[string]any{
		"id": "r", "capacity": []int64{1},
	})
	doJSON(t, hs, "POST", "/v1/reservations", map[string]any{
		"id": "a", "resource": "r",
		"interval": map[string]any{
			"start": "2030-01-01T00:00:00Z",
			"end":   "2030-01-01T01:00:00Z",
		},
		"demand": []int64{1},
	})
	status, body := doJSON(t, hs, "GET", "/v1/events", nil)
	evs := body["events"].([]any)
	lastSeq := int64(evs[len(evs)-1].(map[string]any)["seq"].(float64))
	if status != http.StatusOK {
		t.Fatalf("events %d", status)
	}
	// after_seq=lastSeq 应为空。
	_, body2 := doJSON(t, hs, "GET", "/v1/events?after_seq="+
		strconv.FormatInt(lastSeq, 10), nil)
	if len(body2["events"].([]any)) != 0 {
		t.Fatalf("after_seq 之后不应再有事件，得到 %v", body2["events"])
	}
}

func TestHTTPMalformedJSON(t *testing.T) {
	_, hs, _ := newTestServer(t)
	req, _ := http.NewRequest("POST", hs.URL+"/v1/resources", strings.NewReader("{not json"))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("坏 JSON 应 400，得到 %d", resp.StatusCode)
	}
}

func TestHTTPWallClockAdvanceRejected(t *testing.T) {
	sch := scheduler.New(nil)
	srv := New(sch, Config{}) // 默认墙钟
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()
	status, _ := doJSON(t, hs, "POST", "/test/clock/advance", map[string]any{
		"to": "2030-01-01T03:00:00Z",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("墙钟模式下推进应 400，得到 %d", status)
	}
}

// 保证 Server 与后台 Runner 可独立使用（这里不启动 Runner，
// 仅确认构造不 panic、Handler 可用）。
func TestServerWithoutBackgroundRunner(t *testing.T) {
	fc := clock.NewFake(testEpoch)
	srv := New(scheduler.New(nil), Config{Clock: fc, Epoch: testEpoch})
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()
	_ = srv.Dispatcher()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_ = ctx
}
