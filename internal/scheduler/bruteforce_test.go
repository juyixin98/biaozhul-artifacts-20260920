package scheduler

import (
	"fmt"
	"math/rand"
	"testing"
)

// 本文件把调度库与“离散小时间轴穷举”参考实现做逐拍对比。
//
// 参考模型 bruteModel 在每个整数时刻 t 上直接维护
//   load[t][res][dim] = 该时刻各维度的总需求
// 可行性就是逐个 t 比对容量；最早可行位置就是从窗口起点
// 开始逐格尝试。时间轴短、场景随机，穷举完全可承受——
// 它独立于生产代码的扫描线/候选点算法，因此能作为
// 交叉验证的神谕（oracle）。

const bruteHorizon = 24 // 离散轴：t = 0..23（“小时”）

type bruteModel struct {
	capmap map[string][]int64
	// load[res][t][dim]
	load map[string][][]int64
	// placed[res] 已放置的预约（id -> iv, demand）
	placed map[string]brutePlaced
}

type brutePlaced struct {
	res    string
	iv     Interval
	demand []int64
}

func newBruteModel() *bruteModel {
	return &bruteModel{
		capmap: map[string][]int64{},
		load:   map[string][][]int64{},
		placed: map[string]brutePlaced{},
	}
}

func (m *bruteModel) addResource(id string, cap []int64) {
	m.capmap[id] = append([]int64(nil), cap...)
	m.load[id] = make([][]int64, bruteHorizon)
	for t := range m.load[id] {
		m.load[id][t] = make([]int64, len(cap))
	}
}

// feasibleAt 是离散轴上的穷举可行性检查。
func (m *bruteModel) feasibleAt(res string, iv Interval, d []int64) bool {
	cap := m.capmap[res]
	for t := iv.Start; t < iv.End; t++ {
		if t < 0 || t >= bruteHorizon {
			return false
		}
		for dim := range cap {
			if m.load[res][t][dim]+d[dim] > cap[dim] {
				return false
			}
		}
	}
	return true
}

// earliest 从 lo 开始逐格穷举最早可行起点。
func (m *bruteModel) earliest(res string, window Interval, dur, lo Ticks, d []int64) (Ticks, bool) {
	for t := lo; t+dur <= window.End; t++ {
		if m.feasibleAt(res, Interval{t, t + dur}, d) {
			return t, true
		}
	}
	return 0, false
}

func (m *bruteModel) place(id, res string, iv Interval, d []int64) {
	m.placed[id] = brutePlaced{res, iv, append([]int64(nil), d...)}
	for t := iv.Start; t < iv.End; t++ {
		for dim := range d {
			m.load[res][t][dim] += d[dim]
		}
	}
}

func (m *bruteModel) remove(id string) bool {
	p, ok := m.placed[id]
	if !ok {
		return false
	}
	delete(m.placed, id)
	for t := p.iv.Start; t < p.iv.End; t++ {
		for dim := range p.demand {
			m.load[p.res][t][dim] -= p.demand[dim]
		}
	}
	return true
}

// randDemand 生成需求向量：约 1/6 概率产生零需求（专门压测
// 零容量/零需求边界），否则各维度随机 0..cap。
func randDemand(rng *rand.Rand, cap []int64) Demand {
	d := make(Demand, len(cap))
	if rng.Intn(6) == 0 {
		return d
	}
	for i, c := range cap {
		if c == 0 {
			d[i] = 0
			continue
		}
		// 偏向小需求，偶尔打满。
		switch rng.Intn(3) {
		case 0:
			d[i] = rng.Int63n(c + 1)
		case 1:
			d[i] = c
		default:
			d[i] = rng.Int63n(2) % min64(c+1, 2)
		}
	}
	return d
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

// TestEarliestMatchesBruteForce 随机场景下，生产实现给出的
// 最早可行起点必须与逐格穷举完全一致。
func TestEarliestMatchesBruteForce(t *testing.T) {
	const seeds = 300
	rng := rand.New(rand.NewSource(20260923))
	for iter := 0; iter < seeds; iter++ {
		s := New(nil)
		m := newBruteModel()

		// 1~3 种资源，1~2 个维度，容量 0~2（刻意压满边界：0 容量高频）。
		nres := 1 + rng.Intn(3)
		resources := make([]string, 0, nres)
		for i := 0; i < nres; i++ {
			id := string(rune('A' + i))
			ndim := 1 + rng.Intn(2)
			cap := make([]int64, ndim)
			for d := range cap {
				if rng.Intn(4) == 0 {
					cap[d] = 0
				} else {
					cap[d] = int64(rng.Intn(3)) // 0,1,2
				}
			}
			if _, err := s.AddResource(Resource{ID: id, Capacity: cap}); err != nil {
				t.Fatal(err)
			}
			m.addResource(id, cap)
			resources = append(resources, id)
		}

		// 随机放置 0~12 条已存在预约（只接受两边都认可的）。
		nseed := rng.Intn(13)
		idn := 0
		for k := 0; k < nseed*2 && idn < nseed; k++ {
			res := resources[rng.Intn(len(resources))]
			cap := m.capmap[res]
			start := Ticks(rng.Intn(bruteHorizon))
			end := start + Ticks(1+rng.Intn(4))
			if end > bruteHorizon {
				continue
			}
			d := randDemand(rng, cap)
			iv := Interval{start, end}
			if !m.feasibleAt(res, iv, d) {
				continue
			}
			id := bruteID(idn)
			idn++
			if _, err := s.Reserve(Request{ID: id, Resource: res, Interval: iv, Demand: d}); err != nil {
				t.Fatalf("iter=%d 参考模型可行但生产拒绝 %s %v d=%v: %v", iter, id, iv, d, err)
			}
			m.place(id, res, iv, d)
		}

		// 12 次随机查询，比较“最早可行起点”。
		for q := 0; q < 12; q++ {
			res := resources[rng.Intn(len(resources))]
			ws := Ticks(rng.Intn(bruteHorizon - 1))
			we := ws + Ticks(1+rng.Intn(bruteHorizon-int(ws)))
			window := Interval{ws, we}
			dur := Ticks(1 + rng.Intn(int(we-ws)))
			lo := ws
			if rng.Intn(2) == 0 {
				lo = ws + Ticks(rng.Intn(int(we-ws)))
			}
			d := randDemand(rng, m.capmap[res])

			// 单条需求超过容量的情况两边都应通过错误/不可行表达，跳过维度不合法输入。
			legal := true
			for dim := range d {
				if d[dim] > m.capmap[res][dim] {
					legal = false
				}
			}
			if !legal {
				continue
			}
			gotStart, gotOK, err := s.EarliestFeasible(res, window, dur, lo, d)
			if err != nil {
				t.Fatalf("iter=%d 查询意外报错: %v", iter, err)
			}
			wantStart, wantOK := m.earliest(res, window, dur, lo, d)
			if gotOK != wantOK || (gotOK && gotStart != wantStart) {
				t.Fatalf(`iter=%d 最早可行不一致:
资源=%s 窗口=%v 时长=%d 下限=%d 需求=%v
生产: start=%d ok=%v
穷举: start=%d ok=%v`, iter, res, window, dur, lo, d, gotStart, gotOK, wantStart, wantOK)
			}
		}
	}
}

func bruteID(n int) string {
	return fmt.Sprintf("seed-%d", n)
}

// uniqueOpID 生成跨迭代不冲突的预约 ID。
func uniqueOpID(iter, k int) string {
	return fmt.Sprintf("i%d-op%d", iter, k)
}

// batchItemID 生成批次条目 ID。
func batchItemID(iter, k int) string {
	return fmt.Sprintf("i%d-b%d", iter, k)
}

// TestFixedReserveMatchesBruteForce 随机固定区间预约：
// 两边对每一笔的接受/拒绝必须一致；接受后逐时刻负载一致。
func TestFixedReserveMatchesBruteForce(t *testing.T) {
	const seeds = 200
	rng := rand.New(rand.NewSource(424242))
	for iter := 0; iter < seeds; iter++ {
		s := New(nil)
		m := newBruteModel()
		// 单一资源、两维，容量随机但固定。
		capv := []int64{int64(rng.Intn(3)), int64(rng.Intn(3))}
		if _, err := s.AddResource(Resource{ID: "R", Capacity: capv}); err != nil {
			t.Fatal(err)
		}
		m.addResource("R", capv)

		for k := 0; k < 40; k++ {
			start := Ticks(rng.Intn(bruteHorizon))
			end := start + Ticks(1+rng.Intn(5))
			if end > bruteHorizon {
				end = bruteHorizon
			}
			d := randDemand(rng, capv)
			iv := Interval{start, end}

			legal := true
			for dim := range d {
				if d[dim] < 0 || d[dim] > capv[dim] {
					legal = false
				}
			}
			want := legal && m.feasibleAt("R", iv, d)
			id := uniqueOpID(iter, k)
			_, err := s.Reserve(Request{ID: id, Interval: iv, Demand: d, Resource: "R"})
			got := err == nil
			if got != want {
				t.Fatalf("iter=%d op=%d 接受性不一致: iv=%v d=%v cap=%v 生产=%v 穷举=%v err=%v",
					iter, k, iv, d, capv, got, want, err)
			}
			if want {
				m.place(id, "R", iv, d)
			}
			// 每次后校验不变量：逐时刻容量不超限，且生产端可列出
			// 的所有预约负载之和等于参考模型。
			checkLoadsAgree(t, s, m, capv)
		}
	}
}

func checkLoadsAgree(t *testing.T, s *Scheduler, m *bruteModel, capv []int64) {
	t.Helper()
	got := make([][]int64, bruteHorizon)
	for ti := range got {
		got[ti] = make([]int64, len(capv))
	}
	for _, r := range s.List(ListFilter{Resource: "R"}) {
		if r.Status.IsTerminal() {
			continue
		}
		for ti := r.Interval.Start; ti < r.Interval.End; ti++ {
			for dim := range capv {
				got[ti][dim] += r.Demand[dim]
			}
		}
	}
	for ti := 0; ti < bruteHorizon; ti++ {
		for dim := range capv {
			if got[ti][dim] != m.load["R"][ti][dim] {
				t.Fatalf("t=%d dim=%d 负载不一致: 生产=%d 穷举=%d",
					ti, dim, got[ti][dim], m.load["R"][ti][dim])
			}
			if got[ti][dim] > capv[dim] {
				t.Fatalf("t=%d dim=%d 负载 %d 超容量 %d", ti, dim, got[ti][dim], capv[dim])
			}
		}
	}
}

// TestBatchMatchesBruteForce 混合批次（固定+自动放置）与
// 穷举神谕对比：批次的整体接受/拒绝，以及成功后的落点。
func TestBatchMatchesBruteForce(t *testing.T) {
	const seeds = 200
	rng := rand.New(rand.NewSource(9977))
	for iter := 0; iter < seeds; iter++ {
		s := New(nil)
		m := newBruteModel()
		capv := []int64{int64(1 + rng.Intn(2)), int64(rng.Intn(2))}
		if _, err := s.AddResource(Resource{ID: "R", Capacity: capv}); err != nil {
			t.Fatal(err)
		}
		m.addResource("R", capv)

		// 预置若干占用。
		for k := 0; k < 6; k++ {
			start := Ticks(rng.Intn(bruteHorizon - 2))
			end := start + Ticks(1+rng.Intn(3))
			d := randDemand(rng, capv)
			iv := Interval{start, end}
			if !m.feasibleAt("R", iv, d) {
				continue
			}
			id := fmt.Sprintf("i%d-seed%d", iter, k)
			if _, err := s.Reserve(Request{ID: id, Resource: "R", Interval: iv, Demand: d}); err != nil {
				t.Fatal(err)
			}
			m.place(id, "R", iv, d)
		}

		// 构造一个 1~5 条的混合批次。
		n := 1 + rng.Intn(5)
		items := make([]BatchItem, 0, n)
		// 期望结果：在参考模型上按同样顺序模拟。
		type wantItem struct {
			auto bool
			id   string
			iv   Interval
			d    []int64
		}
		wants := make([]wantItem, 0, n)
		wantFeasible := true
		// 参考模型上批次内的临时占位。
		temp := map[string]brutePlaced{}

		for k := 0; k < n; k++ {
			d := randDemand(rng, capv)
			id := batchItemID(iter, k)
			auto := rng.Intn(2) == 0
			if auto {
				ws := Ticks(rng.Intn(bruteHorizon - 1))
				we := ws + Ticks(1+rng.Intn(bruteHorizon-int(ws)))
				dur := Ticks(1 + rng.Intn(int(we-ws)))
				items = append(items, BatchItem{Place: &PlacementRequest{
					ID: id, Resource: "R",
					Window: Interval{ws, we}, Duration: dur, Demand: d,
				}})
				start, ok := m.earliest("R", Interval{ws, we}, dur, ws, d)
				if !ok {
					wantFeasible = false
				} else {
					iv := Interval{start, start + dur}
					// 临时计入参考模型供后续条目判断。
					m.place("@tmp"+id, "R", iv, d)
					temp["@tmp"+id] = brutePlaced{"R", iv, append([]int64(nil), d...)}
					wants = append(wants, wantItem{auto: true, id: id, iv: iv, d: d})
				}
			} else {
				start := Ticks(rng.Intn(bruteHorizon))
				end := start + Ticks(1+rng.Intn(4))
				if end > bruteHorizon {
					end = bruteHorizon
				}
				iv := Interval{start, end}
				items = append(items, BatchItem{Fixed: &Request{
					ID: id, Resource: "R", Interval: iv, Demand: d,
				}})
				if !m.feasibleAt("R", iv, d) {
					wantFeasible = false
				} else {
					m.place("@tmp"+id, "R", iv, d)
					temp["@tmp"+id] = brutePlaced{"R", iv, append([]int64(nil), d...)}
					wants = append(wants, wantItem{auto: false, id: id, iv: iv, d: d})
				}
			}
		}

		got, err := s.Batch(items)
		gotFeasible := err == nil
		if gotFeasible != wantFeasible {
			t.Fatalf("iter=%d 批次接受性不一致: 生产=%v 穷举=%v err=%v\nitems=%+v\ncap=%v",
				iter, gotFeasible, wantFeasible, err, items, capv)
		}
		if !wantFeasible {
			// 关键：整批回滚——负载与参考模型（去掉临时占位）一致，
			// 且任何批次 ID 都不存在。
			for id := range temp {
				m.remove(id)
			}
			for _, it := range items {
				var id string
				if it.Fixed != nil {
					id = it.Fixed.ID
				} else {
					id = it.Place.ID
				}
				if _, err := s.Get(id); err == nil {
					t.Fatalf("iter=%d 失败批次条目 %s 部分落地", iter, id)
				}
			}
			checkLoadsAgree(t, s, m, capv)
			continue
		}

		// 成功：把临时占位换成真实 ID，并比较落点。
		gotByID := map[string]*Reservation{}
		for _, r := range got {
			gotByID[r.ID] = r
			m.remove("@tmp" + r.ID)
			m.place(r.ID, "R", r.Interval, append([]int64(nil), r.Demand...))
		}
		for _, w := range wants {
			r, ok := gotByID[w.id]
			if !ok {
				t.Fatalf("iter=%d 成功批次缺少 %s", iter, w.id)
			}
			if r.Interval != w.iv {
				t.Fatalf("iter=%d 条目 %s 落点不一致: 生产=%v 穷举=%v",
					iter, w.id, r.Interval, w.iv)
			}
		}
		checkLoadsAgree(t, s, m, capv)
	}
}

// TestDeterministicEarliestCases 若干手工构造的边界场景，
// 保证候选点算法不遗漏“占用结束点”之外的微妙情况。
func TestDeterministicEarliestCases(t *testing.T) {
	s := New(nil)
	if _, err := s.AddResource(Resource{ID: "r", Capacity: []int64{2}}); err != nil {
		t.Fatal(err)
	}
	// 容量 2：[4,6) 占 2 格容量；要 2 格容量、时长 4 的预约，
	// 起点 0 可行（[0,4) 空），即使 [4,6) 满载。
	mustReserve(t, s, Request{ID: "x", Resource: "r", Interval: Interval{4, 6}, Demand: Demand{2}})
	if start, ok, _ := s.EarliestFeasible("r", Interval{0, 10}, 4, 0, Demand{2}); !ok || start != 0 {
		t.Fatalf("期望 start=0 ok=true，得到 %d %v", start, ok)
	}
	// 需求 2、时长 4，窗口 [2,10)：[2,4) 可行但 [4,6) 超载，
	// 下一个候选 6：[6,10) 可行 -> 6。
	if start, ok, _ := s.EarliestFeasible("r", Interval{2, 10}, 4, 0, Demand{2}); !ok || start != 6 {
		t.Fatalf("期望 start=6，得到 %d ok=%v", start, ok)
	}
	// 两段相邻满载 [1,3)[3,5)，需求 2 时长 1：
	// 起点 0 可行（在它们之前）。
	mustReserve(t, s, Request{ID: "y", Resource: "r", Interval: Interval{1, 3}, Demand: Demand{2}})
	if start, ok, _ := s.EarliestFeasible("r", Interval{0, 10}, 1, 0, Demand{2}); !ok || start != 0 {
		t.Fatalf("期望 start=0，得到 %d ok=%v", start, ok)
	}
}
