package batch

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestStress_ConcurrentSubmitAndCancel 在高并发提交/取消混合下验证：
// 每个请求恰好得到一次终态，且调度器计数守恒
// （admitted == succeeded + failed + canceled）。
func TestStress_ConcurrentSubmitAndCancel(t *testing.T) {
	clk := NewFakeClock(time.Unix(1_700_000_000, 0))
	exec := &recordingExec{}
	b, err := New[*testItem](exec, Config{
		MaxCount:      8,
		MaxBatchBytes: 10_000,
		MaxItemBytes:  1000,
		MaxWait:       5 * time.Millisecond,
		Clock:         clk,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = b.Shutdown(ctx)
	})

	var okN, canceledN, failedN, rejectedN atomic.Int64
	var wg sync.WaitGroup

	stopClock := make(chan struct{})
	clockWG := sync.WaitGroup{}
	clockWG.Add(1)
	go func() {
		defer clockWG.Done()
		tk := time.NewTicker(time.Millisecond)
		defer tk.Stop()
		for {
			select {
			case <-tk.C:
				clk.Advance(2 * time.Millisecond)
			case <-stopClock:
				return
			}
		}
	}()

	const workers = 16
	const perWorker = 120
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWorker; i++ {
				n := w*perWorker + i
				ctx, cancel := context.WithCancel(context.Background())
				item := &testItem{
					id:   fmt.Sprintf("n%d", n),
					key:  fmt.Sprintf("model-%d", n%5),
					size: 1 + n%300,
					fail: n%11 == 0,
				}
				if n%7 == 0 {
					// 一部分请求在提交后极短时间内取消。
					go func() {
						time.Sleep(time.Duration(n%3) * time.Millisecond)
						cancel()
					}()
				}
				_, serr := b.Submit(ctx, item)
				cancel()
				switch {
				case serr == nil:
					okN.Add(1)
				case errors.Is(serr, context.Canceled) || errors.Is(serr, ErrExecCanceled):
					canceledN.Add(1)
				default:
					if _, isLarge := AsItemTooLarge(serr); isLarge {
						rejectedN.Add(1)
					} else {
						failedN.Add(1)
					}
				}
			}
		}(w)
	}
	wg.Wait()
	close(stopClock)
	clockWG.Wait()

	total := okN.Load() + canceledN.Load() + failedN.Load() + rejectedN.Load()
	if total != int64(workers*perWorker) {
		t.Fatalf("terminal states = %d (ok=%d canceled=%d failed=%d rejected=%d), want %d",
			total, okN.Load(), canceledN.Load(), failedN.Load(), rejectedN.Load(),
			workers*perWorker)
	}

	// 再推进一个等待窗口，确保已入队但未发的批全部执行完毕，然后核对守恒。
	clk.Advance(10 * time.Millisecond)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		st := b.Stats()
		if st.Admitted == st.Succeeded+st.Failed+st.Canceled {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	st := b.Stats()
	if st.Admitted != st.Succeeded+st.Failed+st.Canceled {
		t.Fatalf("admitted=%d but succeeded+failed+canceled=%d (stats=%+v)",
			st.Admitted, st.Succeeded+st.Failed+st.Canceled, st)
	}
	if st.Rejected != rejectedN.Load() {
		t.Fatalf("rejected stat=%d client-observed=%d", st.Rejected, rejectedN.Load())
	}
	t.Logf("stress stats: %+v", st)
}
