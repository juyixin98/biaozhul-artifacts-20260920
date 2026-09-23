package deque_test

import (
	"sync"
	"sync/atomic"
	"testing"

	"worksteal/internal/deque"
)

// TestSequentialOwnOps 验证所有者视角的 LIFO：压入若干后按逆序弹出。
func TestSequentialOwnOps(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			q := makeDeque[int](kind, 4)
			if _, ok := q.PopBottom(); ok {
				t.Fatal("empty deque popped")
			}
			if _, ok := q.Steal(); ok {
				t.Fatal("empty deque stole")
			}
			for i := 1; i <= 100; i++ {
				v := i
				q.PushBottom(&v)
				if got := q.Len(); got != i {
					t.Fatalf("Len=%d want %d", got, i)
				}
			}
			for i := 100; i >= 1; i-- {
				v, ok := q.PopBottom()
				if !ok || *v != i {
					t.Fatalf("pop got %v ok=%v, want %d", v, ok, i)
				}
			}
		})
	}
}

// TestStealIsFIFO 验证窃取端取到的是最早压入（top）的元素。
func TestStealIsFIFO(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			q := makeDeque[int](kind, 2)
			for i := 1; i <= 50; i++ {
				v := i
				q.PushBottom(&v)
			}
			for i := 1; i <= 50; i++ {
				v, ok := q.Steal()
				if !ok || *v != i {
					t.Fatalf("steal got %v ok=%v want %d", v, ok, i)
				}
			}
		})
	}
}

// TestGrow 验证 ChaseLev 扩容后旧元素仍完整（下标单调、翻倍复制）。
func TestGrow(t *testing.T) {
	q := deque.NewChaseLev[int](2) // 初始容量 4
	for i := 0; i < 1000; i++ {
		v := i
		q.PushBottom(&v)
	}
	got := 0
	for {
		v, ok := q.Steal()
		if !ok {
			break
		}
		if *v != got {
			t.Fatalf("at %d got %d", got, *v)
		}
		got++
	}
	if got != 1000 {
		t.Fatalf("only drained %d elements after grow", got)
	}
}

// TestStealVsPopCompetition 单元素时 owner 与一个 thief 恰好只有一个赢。
func TestStealVsPopCompetition(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			for iter := 0; iter < 2000; iter++ {
				q := makeDeque[int](kind, 4)
				v := 42
				q.PushBottom(&v)
				var winner int32
				start := make(chan struct{})
				var wg sync.WaitGroup
				wg.Add(2)
				go func() {
					defer wg.Done()
					<-start
					if x, ok := q.PopBottom(); ok {
						if *x != 42 {
							t.Errorf("owner bad value %d", *x)
						}
						atomic.AddInt32(&winner, 1)
					}
				}()
				go func() {
					defer wg.Done()
					<-start
					if x, ok := q.Steal(); ok {
						if *x != 42 {
							t.Errorf("thief bad value %d", *x)
						}
						atomic.AddInt32(&winner, 1)
					}
				}()
				close(start)
				wg.Wait()
				if winner != 1 {
					t.Fatalf("iteration %d: winners=%d, want exactly 1", iter, winner)
				}
			}
		})
	}
}

// TestConcurrentExactlyOnce 高竞争下压入 N 个元素，owner pop + 多 thief steal
// 合计必须恰好取到 N 个互不重复的元素。配合 -race 使用。
func TestConcurrentExactlyOnce(t *testing.T) {
	for _, kind := range []string{"chaselev", "mutex"} {
		t.Run(kind, func(t *testing.T) {
			q := makeDeque[int](kind, 4)
			const N = 200_000
			const thieves = 4
			var taken sync.Map // value -> struct{}
			var count atomic.Int64
			var wg sync.WaitGroup

			// 生产者（唯一 owner push + pop bottom）。
			producerDone := make(chan struct{})
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < N; i++ {
					v := i
					q.PushBottom(&v)
					if i%3 == 0 {
						if x, ok := q.PopBottom(); ok {
							recordOnce(t, &taken, &count, *x)
						}
					}
				}
				close(producerDone)
			}()
			// 窃取者。
			for th := 0; th < thieves; th++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					for {
						if x, ok := q.Steal(); ok {
							recordOnce(t, &taken, &count, *x)
							continue
						}
						select {
						case <-producerDone:
							// 生产者结束后再排空剩余。
							if x, ok := q.Steal(); ok {
								recordOnce(t, &taken, &count, *x)
								continue
							}
							return
						default:
						}
					}
				}()
			}
			wg.Wait()
			if count.Load() != N {
				t.Fatalf("taken %d, want %d", count.Load(), N)
			}
		})
	}
}

func recordOnce(t *testing.T, m *sync.Map, count *atomic.Int64, v int) {
	t.Helper()
	if _, loaded := m.LoadOrStore(v, struct{}{}); loaded {
		t.Errorf("value %d taken more than once", v)
	}
	count.Add(1)
}

func makeDeque[T any](kind string, logSize uint) deque.Deque[T] {
	if kind == "mutex" {
		return deque.NewMutex[T](1 << logSize)
	}
	return deque.NewChaseLev[T](logSize)
}
