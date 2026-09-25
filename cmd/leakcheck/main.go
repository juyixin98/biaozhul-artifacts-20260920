// Command leakcheck creates and destroys many pools exercising resize and
// both shutdown modes, then reports runtime goroutine counts. A non-decreasing
// count across cycles indicates a worker/drainer leak.
package main

import (
	"context"
	"fmt"
	"os"
	"runtime"
	"sync/atomic"
	"time"

	"dynpool"
)

func main() {
	runtime.GC()
	var baseline int
	for i := 0; i < 3; i++ {
		runtime.GC()
		time.Sleep(20 * time.Millisecond)
		baseline = runtime.NumGoroutine()
	}
	fmt.Printf("baseline goroutines: %d\n", baseline)

	var totalAccepted, totalCompleted atomic.Int64
	for cycle := 0; cycle < 20; cycle++ {
		p, err := dynpool.New(dynpool.Config{
			Name:      fmt.Sprintf("c%d", cycle),
			Workers:   4,
			QueueSize: 128,
			Reject:    dynpool.PolicyAbort,
		})
		if err != nil {
			panic(err)
		}
		// Resize up/down/up interleaved with work.
		_ = p.Resize(8)
		for j := 0; j < 50; j++ {
			j := j
			h, err := p.Submit(func(ctx context.Context) error {
				time.Sleep(time.Millisecond)
				if j%7 == 0 {
					// occasionally block briefly on ctx
					select {
					case <-time.After(2 * time.Millisecond):
					case <-ctx.Done():
					}
				}
				totalCompleted.Add(1)
				return nil
			})
			if err == nil {
				totalAccepted.Add(1)
			}
			_ = h
		}
		_ = p.Resize(2)
		_ = p.Resize(6)
		if cycle%4 == 3 {
			dropped := p.ShutdownNow()
			totalAccepted.Add(-int64(len(dropped)))
		} else {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			if err := p.Shutdown(ctx); err != nil {
				panic(err)
			}
			cancel()
		}
	}

	runtime.GC()
	time.Sleep(100 * time.Millisecond)
	after := runtime.NumGoroutine()
	fmt.Printf("after 20 create/destroy cycles: %d goroutines\n", after)
	fmt.Printf("accepted=%d completed=%d (force cycles dropped tasks are excluded)\n",
		totalAccepted.Load(), totalCompleted.Load())
	if after-baseline > 2 {
		fmt.Printf("LEAK SUSPECTED: +%d goroutines\n", after-baseline)
		os.Exit(1)
	}
	fmt.Println("OK: no goroutine leak")
}
