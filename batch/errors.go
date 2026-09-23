package batch

import (
	"errors"
	"fmt"
)

var (
	// ErrItemTooLarge 表示单项请求的字节数超过 MaxItemBytes：
	// 超大项不会进入任何批，也不会影响批中其它请求。
	ErrItemTooLarge = errors.New("batch: item exceeds max item bytes")
	// ErrShuttingDown 表示调度器正在关闭，不再接受新请求。
	ErrShuttingDown = errors.New("batch: batcher is shutting down")
	// ErrExecCanceled 表示请求方在执行期间取消，仅影响其自身。
	ErrExecCanceled = errors.New("batch: request canceled while in flight")
	// ErrExecutorResultMismatch 表示执行器返回的结果数量与入批条目数量不一致，
	// 属于执行器实现错误。
	ErrExecutorResultMismatch = errors.New("batch: executor returned unexpected number of results")
)

// ItemTooLargeError 携带超限详情，便于上层映射为 HTTP 413。
type ItemTooLargeError struct {
	Size, Max int
}

func (e *ItemTooLargeError) Error() string {
	return fmt.Sprintf("%s: size=%d max=%d", ErrItemTooLarge, e.Size, e.Max)
}

func (e *ItemTooLargeError) Unwrap() error { return ErrItemTooLarge }

// AsItemTooLarge 从任意错误中提取 *ItemTooLargeError。
func AsItemTooLarge(err error) (*ItemTooLargeError, bool) {
	var e *ItemTooLargeError
	return e, errors.As(err, &e)
}
