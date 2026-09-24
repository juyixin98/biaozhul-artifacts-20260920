package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/example/robot-task/internal/api"
)

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func validBody() map[string]any {
	return map[string]any{
		"robots": []map[string]any{{
			"id": "r1", "start": map[string]int64{"x": 0, "y": 0},
			"start_battery": 10, "battery_capacity": 20,
			"capacity": 10, "energy_per_dist": 1, "charge_rate": 1,
		}},
		"depot": map[string]int64{"x": 0, "y": 0},
		"chargers": []map[string]any{{
			"id": "c1", "location": map[string]int64{"x": 5, "y": 0},
		}},
		"tasks": []map[string]any{{
			"id": "t1", "location": map[string]int64{"x": 15, "y": 0},
			"load": 1, "ready": 0, "due": 1000, "service_time": 0,
		}},
	}
}

func post(t *testing.T, body []byte) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/plan", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	api.Handler().ServeHTTP(rec, req)
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON: %v, body=%s", err, rec.Body.String())
	}
	return rec, out
}

func TestPlanFeasible(t *testing.T) {
	rec, out := post(t, mustJSON(t, validBody()))
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码=%d body=%s", rec.Code, rec.Body.String())
	}
	if out["feasible"] != true {
		t.Fatalf("期望 feasible, 实际 %v: %v", out["feasible"], out["reason"])
	}
	if out["makespan"].(float64) != 30 {
		t.Fatalf("期望 makespan 30, 实际 %v", out["makespan"])
	}
	val, ok := out["validation"].(map[string]any)
	if !ok || val["valid"] != true {
		t.Fatalf("响应应附带通过的独立校验结果, 实际 %v", out["validation"])
	}
	plans := out["plans"].([]any)
	if len(plans) != 1 {
		t.Fatalf("应有 1 条机器人计划, 实际 %d", len(plans))
	}
}

func TestPlanInfeasibleTightWindow(t *testing.T) {
	body := validBody()
	tasks := body["tasks"].([]map[string]any)
	tasks[0]["due"] = 5
	rec, out := post(t, mustJSON(t, body))
	if rec.Code != http.StatusOK {
		t.Fatalf("无解应返回 200 + feasible=false, 实际 %d", rec.Code)
	}
	if out["feasible"] != false {
		t.Fatalf("期望 feasible=false, 实际 %v", out["feasible"])
	}
	if out["optimal"] != true {
		t.Fatalf("小实例无解应为已证明 optimal=true")
	}
	if _, ok := out["plans"]; ok {
		t.Fatal("无解时不应返回 plans")
	}
}

func TestBadJSON(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/plan", strings.NewReader("{not json"))
	rec := httptest.NewRecorder()
	api.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("坏 JSON 应 400, 实际 %d", rec.Code)
	}
}

func TestUnknownField(t *testing.T) {
	body := validBody()
	body["bogus"] = 1
	rec, _ := post(t, mustJSON(t, body))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("未知字段应 400, 实际 %d", rec.Code)
	}
}

func TestValidationErrors(t *testing.T) {
	cases := []func(map[string]any){
		// 空机器人
		func(b map[string]any) { b["robots"] = []any{} },
		// 初始电量超容量
		func(b map[string]any) {
			rs := b["robots"].([]map[string]any)
			rs[0]["start_battery"] = 99
		},
		// ready > due
		func(b map[string]any) {
			ts := b["tasks"].([]map[string]any)
			ts[0]["ready"], ts[0]["due"] = 100, 5
		},
		// id 重复
		func(b map[string]any) {
			rs := b["robots"].([]map[string]any)
			rs = append(rs, map[string]any{
				"id": "r1", "start": map[string]int64{"x": 0, "y": 0},
				"start_battery": 5, "battery_capacity": 20,
				"capacity": 10, "energy_per_dist": 1, "charge_rate": 1,
			})
			b["robots"] = rs
		},
		// 有任务但无充电点
		func(b map[string]any) { b["chargers"] = []any{} },
	}
	for i, mut := range cases {
		b := validBody()
		mut(b)
		rec, _ := post(t, mustJSON(t, b))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("非法输入用例 %d 应 400, 实际 %d body=%s", i, rec.Code, rec.Body.String())
		}
	}
}

func TestMethodNotAllowed(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/plan", nil)
	rec := httptest.NewRecorder()
	api.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET 应 405, 实际 %d", rec.Code)
	}
}

func TestHealthz(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	api.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz 应 200, 实际 %d", rec.Code)
	}
}
