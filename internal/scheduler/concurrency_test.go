package scheduler

import (
	"sync"
	"testing"
)

// TestConcurrentReserveNoOverCapacity 高并发下容量约束绝不可被破坏：
// 容量 N、M 个 goroutine 同时预约同一区间，恰好 N 个成功。
func TestConcurrentReserveNoOverCapacity(t *testing.T) {
	const capacity = 3
	const goroutines = 50
	sch := New(nil)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{capacity}}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	succeeded := 0
	start := make(chan struct{})
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := sch.Reserve(Request{
				Resource: "r",
				Interval: Interval{0, 10},
				Demand:   Demand{1},
			})
			if err == nil {
				mu.Lock()
				succeeded++
				mu.Unlock()
			}
		}()
	}
	close(start)
	wg.Wait()
	if succeeded != capacity {
		t.Fatalf("并发预约成功数期望 %d，得到 %d", capacity, succeeded)
	}
	// 落地数与逐时刻负载复核。
	active := sch.List(ListFilter{Resource: "r"})
	if len(active) != capacity {
		t.Fatalf("活动预约数期望 %d，得到 %d", capacity, len(active))
	}
}

// TestConcurrentBatchAtomic 并发批次：成功批次落地的条目之间
// 以及与既有预约之间都不得出现容量超限。
func TestConcurrentBatchAtomic(t *testing.T) {
	sch := New(nil)
	// 容量 1，每个批次含两条相邻预约（占用 2 格）。
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	const batches = 30
	var wg sync.WaitGroup
	start := make(chan struct{})
	successes := make(chan int, batches)
	for i := 0; i < batches; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			items := []BatchItem{
				{Fixed: &Request{Resource: "r", Interval: Interval{0, 1}, Demand: Demand{1}}},
				{Fixed: &Request{Resource: "r", Interval: Interval{1, 2}, Demand: Demand{1}}},
			}
			got, err := sch.Batch(items)
			if err == nil {
				successes <- len(got)
			} else {
				successes <- 0
			}
		}(i)
	}
	close(start)
	wg.Wait()
	close(successes)
	totalLanded := 0
	successBatches := 0
	for n := range successes {
		if n > 0 {
			successBatches++
			totalLanded += n
		}
	}
	// 容量 1、两个时段，每个成功批次恰好打满两格：最多 1 个批次成功。
	if successBatches > 1 {
		t.Fatalf("原子性被破坏：%d 个批次同时落地", successBatches)
	}
	if totalLanded != 2*successBatches {
		t.Fatalf("落地总数 %d 与成功批次数 %d 不符", totalLanded, successBatches)
	}
	if len(sch.List(ListFilter{})) != totalLanded {
		t.Fatalf("实际存储预约数与报告不符")
	}
}

// TestConcurrentExplicitIDUnique 显式 ID 唯一性在并发下必须成立：
// 同一 ID 的多笔预约（区间互不冲突，绕过容量检查）恰好 1 笔成功。
func TestConcurrentExplicitIDUnique(t *testing.T) {
	sch := New(nil)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{100}}); err != nil {
		t.Fatal(err)
	}
	const n = 40
	var wg sync.WaitGroup
	start := make(chan struct{})
	successes := make(chan int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			// 每人不同区间，避免因容量竞争而失败——只考验 ID 唯一性。
			_, err := sch.Reserve(Request{
				ID:       "same-id",
				Resource: "r",
				Interval: Interval{Ticks(i), Ticks(i + 1)},
				Demand:   Demand{1},
			})
			if err == nil {
				successes <- 1
			} else {
				successes <- 0
			}
		}(i)
	}
	close(start)
	wg.Wait()
	close(successes)
	wins := 0
	for x := range successes {
		wins += x
	}
	if wins != 1 {
		t.Fatalf("同一显式 ID 应恰好 1 笔成功，得到 %d", wins)
	}
}

// TestConcurrentBatchExplicitIDUnique 并发批次与单笔预约竞争同一 ID，
// 不允许出现重复 ID（Batch 必须全程持锁）。
func TestConcurrentBatchExplicitIDUnique(t *testing.T) {
	sch := New(nil)
	if _, err := sch.AddResource(Resource{ID: "r", Capacity: []int64{100}}); err != nil {
		t.Fatal(err)
	}
	const n = 30
	var wg sync.WaitGroup
	start := make(chan struct{})
	wins := make(chan bool, 2*n)
	launch := func(fn func() error) {
		defer wg.Done()
		<-start
		wins <- fn() == nil
	}
	for i := 0; i < n; i++ {
		wg.Add(2)
		go launch(func() error {
			_, err := sch.Reserve(Request{
				ID: "dup", Resource: "r",
				Interval: Interval{Ticks(200 + i), Ticks(201 + i)}, Demand: Demand{1},
			})
			return err
		})
		go launch(func() error {
			_, err := sch.Batch([]BatchItem{{Fixed: &Request{
				ID: "dup", Resource: "r",
				Interval: Interval{Ticks(400 + i), Ticks(401 + i)}, Demand: Demand{1},
			}}})
			return err
		})
	}
	close(start)
	wg.Wait()
	close(wins)
	count := 0
	for w := range wins {
		if w {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("竞争同一 ID 应恰好 1 个操作成功，得到 %d", count)
	}
}
