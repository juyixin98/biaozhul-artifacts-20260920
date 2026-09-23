package executor

import (
	"runtime"
	"sync/atomic"
	"time"
)

func defaultWorkers() int {
	n := runtime.NumCPU()
	if n < 1 {
		return 1
	}
	return n
}

// fastrand 是无锁的进程内 xorshift 随机源（窃取起点抖动用，质量要求低）。
var rngState atomic.Uint64

func fastrand() uint32 {
	for {
		old := rngState.Load()
		x := old
		if x == 0 {
			x = uint64(time.Now().UnixNano()) | 1
		}
		x ^= x << 13
		x ^= x >> 7
		x ^= x << 17
		if rngState.CompareAndSwap(old, x) {
			return uint32(x >> 32)
		}
	}
}
