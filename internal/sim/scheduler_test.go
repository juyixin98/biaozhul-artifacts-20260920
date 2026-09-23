package sim

import "testing"

// TestDeterministicReplay 验证相同种子下事件序列逐 tick 可复现。
func TestDeterministicReplay(t *testing.T) {
	run := func() []int64 {
		s := New(12345)
		var got []int64
		for i := 0; i < 5; i++ {
			s.Schedule("a", "x", int64(10+i*3), func() { got = append(got, s.Now()) })
			s.Schedule("b", "x", int64(1+i*3), func() { got = append(got, s.Now()+1000) })
		}
		s.Run(100)
		return got
	}
	a := run()
	b := run()
	if len(a) != len(b) || len(a) != 10 {
		t.Fatalf("事件数量错误: %d vs %d", len(a), len(b))
	}
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("重复运行不确定: 位置 %d: %d != %d", i, a[i], b[i])
		}
	}
	// 验证按时刻排序（b 的 1+i*3 先于 a 的 10+i*3）。
	if a[0] != 1+1000 || a[1] != 4+1000 || a[2] != 7+1000 {
		t.Fatalf("事件未按 tick 排序: %v", a[:3])
	}
}

// TestDeterministicRng 验证随机源按种子确定性复现（网络故障依赖它）。
func TestDeterministicRng(t *testing.T) {
	draw := func(seed int64) []float64 {
		s := New(seed)
		out := make([]float64, 8)
		for i := range out {
			out[i] = s.Rng().Float64()
		}
		return out
	}
	a, b, c := draw(12345), draw(12345), draw(777)
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("同种子随机序列不一致: #%d %v != %v", i, a[i], b[i])
		}
		if a[i] == c[i] {
			t.Fatalf("不同种子随机序列不应逐项相同（极小概率误报）")
		}
	}
}

func TestCancelNode(t *testing.T) {
	s := New(1)
	fired := map[string]bool{}
	s.Schedule("a", "timer", 5, func() { fired["a-timer"] = true })
	s.ScheduleFrom("b", "a", "message", 6, func() { fired["a->b"] = true }) // a 发出在途
	s.ScheduleFrom("a", "c", "message", 7, func() { fired["c->a"] = true }) // 发给 a
	s.Schedule("b", "timer", 4, func() { fired["b-timer"] = true })

	// tick 4 b 的定时器先触发；紧接着 a 崩溃。
	s.Schedule("system", "crash", 4, func() {
		owned, inFlight := s.CancelNode("a")
		if owned != 2 || inFlight != 1 {
			t.Fatalf("撤销计数错误: owned=%d inFlight=%d", owned, inFlight)
		}
	})
	s.Run(20)
	if fired["a-timer"] || fired["a->b"] || fired["c->a"] {
		t.Fatalf("崩溃后 a 相关事件不应触发: %v", fired)
	}
	if !fired["b-timer"] {
		t.Fatal("b 的定时器应当触发")
	}
}
