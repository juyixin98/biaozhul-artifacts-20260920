// Package stub 是受控指标桩服务：按场景产生时间桶指标，支持迟到/订正数据。
package stub

import (
	"encoding/json"
	"math"
	"net/http"
	"sync"
	"time"

	"github.com/go-chi/chi/v5"
)

type bucketSpec struct {
	req int64
	err int64
	p95 float64
	// preliminary：首版数据；final：迟到的订正数据（late_success 场景）
	prelimAvailAt time.Time
	finalReq      int64
	finalErr      int64
	finalP95      float64
	finalAvailAt  time.Time
}

type scenarioState struct {
	Name            string    `json:"name"`
	Service         string    `json:"service"`
	StartedAt       time.Time `json:"started_at"`
	BucketSeconds   int64     `json:"bucket_seconds"`
	CorrectionDelay time.Duration
	specs           func(idx int64, start, bucketEnd time.Time, s *scenarioState) bucketSpec
}

var scenarioDefs = map[string]struct {
	desc string
	spec func(idx int64, start, bucketEnd time.Time, s *scenarioState) bucketSpec
}{
	// 全程健康
	"healthy": {
		desc: "error≈0.0%, p95=120ms, 100 请求/桶",
		spec: func(idx int64, _, _ time.Time, _ *scenarioState) bucketSpec {
			return healthyBucket
		},
	},
	// 短暂尖峰：第 2 个桶错误率 8% / p95 900ms，随后立刻恢复；
	// 多桶聚合后仍在阈值内，不阻断推进。
	"transient_spike": {
		desc: "第 2 个桶 8% 错误率 + 900ms 尖峰，之后恢复健康",
		spec: func(idx int64, _, _ time.Time, _ *scenarioState) bucketSpec {
			if idx == 1 {
				return bucketSpec{req: 100, err: 8, p95: 900}
			}
			return healthyBucket
		},
	},
	// 持续退化：每桶都超阈值
	"sustained_degradation": {
		desc: "持续 6% 错误率，p95=800ms",
		spec: func(idx int64, _, _ time.Time, _ *scenarioState) bucketSpec {
			return bucketSpec{req: 100, err: 6, p95: 800}
		},
	},
	// 样本不足：每桶仅 5 个请求
	"insufficient_samples": {
		desc: "仅 5 请求/桶，样本量不足",
		spec: func(idx int64, _, _ time.Time, _ *scenarioState) bucketSpec {
			return bucketSpec{req: 5, err: 0, p95: 120}
		},
	},
	// 指标中断：无任何数据，应判「未知」而非健康
	"metrics_outage": {
		desc: "无任何指标上报",
		spec: nil,
	},
	// 回退后迟到成功：首版数据显示 10% 错误率（导致回退），
	// 订正延迟后同一批桶被替换为健康数据（迟到成功证据）。
	"late_success": {
		desc: "首版 10% 错误率，延迟后订正为健康数据（回退后迟到成功）",
		spec: func(idx int64, start, bucketEnd time.Time, s *scenarioState) bucketSpec {
			return bucketSpec{
				req:          100,
				err:          10,
				p95:          900,
				finalReq:     100,
				finalErr:     0,
				finalP95:     120,
				finalAvailAt: s.StartedAt.Add(s.CorrectionDelay),
			}
		},
	},
}

var healthyBucket = bucketSpec{req: 100, err: 0, p95: 120}

// Stub 是指标桩服务，可注入时钟用于确定性测试。
type Stub struct {
	mu       sync.Mutex
	services map[string]*scenarioState
	Now      func() time.Time
}

func New() *Stub {
	return &Stub{services: map[string]*scenarioState{}, Now: time.Now}
}

func (s *Stub) Router() http.Handler {
	r := chi.NewRouter()
	r.Get("/metrics", s.getMetrics)
	r.Post("/scenario", s.setScenario)
	r.Get("/scenario", s.getScenario)
	r.Get("/scenarios", s.listScenarios)
	r.Delete("/scenario", s.deleteScenario)
	return r
}

type setScenarioReq struct {
	Service                string    `json:"service"`
	Name                   string    `json:"name"`
	BucketSeconds          int64     `json:"bucket_seconds"`
	CorrectionDelaySeconds int64     `json:"correction_delay_seconds"`
	StartedAt              time.Time `json:"started_at"`
}

func (s *Stub) setScenario(w http.ResponseWriter, r *http.Request) {
	var req setScenarioReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	def, ok := scenarioDefs[req.Name]
	if !ok {
		writeErr(w, http.StatusBadRequest, "unknown scenario: "+req.Name)
		return
	}
	if req.Service == "" {
		writeErr(w, http.StatusBadRequest, "service is required")
		return
	}
	if req.BucketSeconds <= 0 {
		req.BucketSeconds = 5
	}
	if req.CorrectionDelaySeconds <= 0 {
		req.CorrectionDelaySeconds = 45
	}
	if req.StartedAt.IsZero() {
		req.StartedAt = s.Now()
	}
	st := &scenarioState{
		Name:            req.Name,
		Service:         req.Service,
		StartedAt:       req.StartedAt,
		BucketSeconds:   req.BucketSeconds,
		CorrectionDelay: time.Duration(req.CorrectionDelaySeconds) * time.Second,
		specs:           def.spec,
	}
	s.mu.Lock()
	s.services[req.Service] = st
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"service":        st.Service,
		"scenario":       st.Name,
		"started_at":     st.StartedAt,
		"bucket_seconds": st.BucketSeconds,
	})
}

func (s *Stub) getScenario(w http.ResponseWriter, r *http.Request) {
	svc := r.URL.Query().Get("service")
	s.mu.Lock()
	st, ok := s.services[svc]
	s.mu.Unlock()
	if !ok {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service":        st.Service,
		"scenario":       st.Name,
		"started_at":     st.StartedAt,
		"bucket_seconds": st.BucketSeconds,
	})
}

func (s *Stub) deleteScenario(w http.ResponseWriter, r *http.Request) {
	svc := r.URL.Query().Get("service")
	s.mu.Lock()
	delete(s.services, svc)
	s.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (s *Stub) listScenarios(w http.ResponseWriter, r *http.Request) {
	out := map[string]string{}
	for name, def := range scenarioDefs {
		out[name] = def.desc
	}
	writeJSON(w, http.StatusOK, out)
}

type outBucket struct {
	Start        string  `json:"start"`
	End          string  `json:"end"`
	Samples      int64   `json:"samples"`
	Errors       int64   `json:"errors"`
	P95LatencyMs float64 `json:"p95_latency_ms"`
	IngestedAt   string  `json:"ingested_at"`
}

func (s *Stub) getMetrics(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	svc := q.Get("service")
	from, err1 := time.Parse(time.RFC3339, q.Get("from"))
	to, err2 := time.Parse(time.RFC3339, q.Get("to"))
	if svc == "" || err1 != nil || err2 != nil || !to.After(from) {
		writeErr(w, http.StatusBadRequest, "need service and valid from/to RFC3339 timestamps")
		return
	}

	s.mu.Lock()
	st := s.services[svc]
	s.mu.Unlock()
	if st == nil {
		w.WriteHeader(http.StatusNoContent) // 无场景 = 无指标
		return
	}

	now := s.Now()
	bkt := time.Duration(st.BucketSeconds) * time.Second
	// 与 [from,to) 相交的桶
	firstIdx := int64(math.Floor(float64(from.Sub(st.StartedAt)) / float64(bkt)))
	if firstIdx < 0 {
		firstIdx = 0
	}

	resp := struct {
		BucketSeconds   int64       `json:"bucket_seconds"`
		ExpectedBuckets int         `json:"expected_buckets"`
		ReceivedBuckets int         `json:"received_buckets"`
		Buckets         []outBucket `json:"buckets"`
		Samples         int64       `json:"samples"`
		Errors          int64       `json:"errors"`
		P95LatencyMs    float64     `json:"p95_latency_ms"`
	}{
		BucketSeconds: st.BucketSeconds,
		Buckets:       []outBucket{},
	}

	// 应到桶 = 已到闭合时间的桶；未闭合桶不算缺失（那是窗口未走完，不是指标丢失）
	var wSum float64
	for idx := firstIdx; ; idx++ {
		bStart := st.StartedAt.Add(time.Duration(idx) * bkt)
		bEnd := bStart.Add(bkt)
		if !bEnd.After(from) {
			continue
		}
		if !bStart.Before(to) {
			break
		}
		if bEnd.After(now) {
			continue // 桶未闭合，不计入应到
		}
		resp.ExpectedBuckets++

		// 桶闭合后数据才可能到达
		prelimAvail := bEnd
		var sp bucketSpec
		if st.specs != nil {
			sp = st.specs(idx, bStart, bEnd, st)
		}
		if sp.req == 0 && sp.finalReq == 0 {
			continue // metrics_outage：应到但无数据
		}
		if !sp.prelimAvailAt.IsZero() {
			prelimAvail = sp.prelimAvailAt
		}

		// 优先使用已到达的订正（迟到成功）数据
		if !sp.finalAvailAt.IsZero() && !sp.finalAvailAt.After(now) {
			resp.Buckets = append(resp.Buckets, outBucket{
				Start: bStart.UTC().Format(time.RFC3339), End: bEnd.UTC().Format(time.RFC3339),
				Samples: sp.finalReq, Errors: sp.finalErr, P95LatencyMs: sp.finalP95,
				IngestedAt: sp.finalAvailAt.UTC().Format(time.RFC3339),
			})
			resp.Samples += sp.finalReq
			resp.Errors += sp.finalErr
			wSum += float64(sp.finalReq) * sp.finalP95
		} else if !prelimAvail.After(now) {
			resp.Buckets = append(resp.Buckets, outBucket{
				Start: bStart.UTC().Format(time.RFC3339), End: bEnd.UTC().Format(time.RFC3339),
				Samples: sp.req, Errors: sp.err, P95LatencyMs: sp.p95,
				IngestedAt: prelimAvail.UTC().Format(time.RFC3339),
			})
			resp.Samples += sp.req
			resp.Errors += sp.err
			wSum += float64(sp.req) * sp.p95
		}
	}
	resp.ReceivedBuckets = len(resp.Buckets)
	if resp.Samples > 0 {
		resp.P95LatencyMs = wSum / float64(resp.Samples)
	}
	writeJSON(w, http.StatusOK, resp)
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}
