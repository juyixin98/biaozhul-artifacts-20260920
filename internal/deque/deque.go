// Package deque 提供工作窃取使用的双端队列。
//
// 所有者（worker 自己）只从 bottom 端 LIFO 压入/弹出（热任务缓存友好），
// 窃取者只从 top 端 FIFO 窃取。两种实现实现同一接口：
//   - ChaseLev：Chase-Levev 风格，push/pop/steal 走原子操作快速路径，
//     仅扩容时取独占锁；
//   - Mutex：单一互斥锁的简单实现，用于对照测试。
package deque

import (
	"sync"
	"sync/atomic"
)

// Deque 是工作窃取双端队列的抽象。元素为指针，零值表示空。
type Deque[T any] interface {
	// PushBottom 仅允许所有者调用：向 bottom 端压入。
	PushBottom(v *T)
	// PopBottom 仅允许所有者调用：从 bottom 端弹出。
	PopBottom() (*T, bool)
	// Steal 允许任意窃取者调用：从 top 端窃取。
	Steal() (*T, bool)
	// Len 返回近似长度（并发下不保证精确）。
	Len() int
}

// ---------- Chase-Lev ----------

// clBuffer 是定长环形缓冲，下标单调递增，用 (i & cap-1) 取槽位。
type clBuffer[T any] struct {
	logSize uint
	data    []atomic.Pointer[T]
}

func newCLBuffer[T any](logSize uint) *clBuffer[T] {
	return &clBuffer[T]{logSize: logSize, data: make([]atomic.Pointer[T], 1<<logSize)}
}

func (b *clBuffer[T]) capacity() int { return len(b.data) }

func (b *clBuffer[T]) put(i int64, v *T) {
	b.data[i&int64(b.capacity()-1)].Store(v)
}

func (b *clBuffer[T]) get(i int64) *T {
	return b.data[i&int64(b.capacity()-1)].Load()
}

// ChaseLev 是 Chase-Lev 工作窃取双端队列。
//
// 不变量（参见 Le, Mendis, Wong, Su, Abdelzaher, "Correct and Efficient
// Work Stealing for Weak Memory Models"）：bottom 只由所有者写，top 由
// 窃取者 CAS 写。所有原子操作均为 Go 的 sequentially consistent 原子量，
// 保证 popBottom 中“先写 bottom 再读 top”的 store-load 顺序。
//
// 与经典定长实现的唯一区别：push 发现容量不足时取 grow 写锁扩容为两倍，
// 期间所有 push/pop/steal 因持有读锁而等待；扩容完成后按相同下标布局
// 复制现存元素。
type ChaseLev[T any] struct {
	bottom atomic.Int64
	top    atomic.Int64
	array  atomic.Pointer[clBuffer[T]]
	grow   sync.RWMutex
}

// NewChaseLev 创建队列，初始容量为 2^initialLogSize（最小 4）。
func NewChaseLev[T any](initialLogSize uint) *ChaseLev[T] {
	if initialLogSize < 2 {
		initialLogSize = 2
	}
	d := &ChaseLev[T]{}
	d.array.Store(newCLBuffer[T](initialLogSize))
	return d
}

func (d *ChaseLev[T]) PushBottom(v *T) {
	d.grow.RLock()
	b := d.bottom.Load()
	a := d.array.Load()
	if b-d.top.Load() >= int64(a.capacity()) {
		d.grow.RUnlock()
		d.growBuffer()
		d.grow.RLock()
		b = d.bottom.Load()
		a = d.array.Load()
	}
	a.put(b, v)
	// Release 语义：元素就位后再发布新的 bottom。
	d.bottom.Store(b + 1)
	d.grow.RUnlock()
}

// growBuffer 取独占锁把缓冲翻倍。调用者不得持有读锁。
// 所有者是唯一的 push 方，但因 push 在检测扩容时释放过读锁，
// 这里重新检查容量以防重复扩容。
func (d *ChaseLev[T]) growBuffer() {
	d.grow.Lock()
	defer d.grow.Unlock()
	a := d.array.Load()
	t := d.top.Load()
	b := d.bottom.Load()
	if b-t < int64(a.capacity()) {
		return
	}
	nb := newCLBuffer[T](a.logSize + 1)
	for i := t; i < b; i++ {
		nb.put(i, a.get(i))
	}
	d.array.Store(nb)
}

func (d *ChaseLev[T]) PopBottom() (*T, bool) {
	d.grow.RLock()
	b := d.bottom.Load() - 1
	a := d.array.Load()
	// 预留槽位：先把 bottom 收缩到 b。
	d.bottom.Store(b)
	t := d.top.Load() // store-load 屏障：必须读到窃取者的最新 top
	switch {
	case b < t: // 预留前即为空，恢复 bottom。
		d.bottom.Store(t)
		d.grow.RUnlock()
		return nil, false
	case b == t: // 只剩一个元素，与窃取者竞争。
		v := a.get(b)
		if !d.top.CompareAndSwap(t, t+1) {
			// 窃取者赢得 CAS，队列恢复为空。
			d.bottom.Store(t + 1)
			d.grow.RUnlock()
			return nil, false
		}
		d.bottom.Store(t + 1)
		d.grow.RUnlock()
		return v, true
	default: // b > t，无竞争。
		v := a.get(b)
		d.grow.RUnlock()
		return v, true
	}
}

func (d *ChaseLev[T]) Steal() (*T, bool) {
	d.grow.RLock()
	t := d.top.Load()
	// 与 popBottom 的 store-load 顺序对应：先 top 后 bottom。
	b := d.bottom.Load()
	a := d.array.Load()
	if t >= b {
		d.grow.RUnlock()
		return nil, false
	}
	v := a.get(t)
	if !d.top.CompareAndSwap(t, t+1) {
		// 所有者取走了最后一个元素，或被别的窃取者抢先。
		d.grow.RUnlock()
		return nil, false
	}
	d.grow.RUnlock()
	return v, true
}

func (d *ChaseLev[T]) Len() int {
	t := d.top.Load()
	b := d.bottom.Load()
	n := int(b - t)
	if n < 0 {
		return 0
	}
	return n
}

// ---------- Mutex（对照实现） ----------

// Mutex 是用单一互斥锁保护的切片双端队列，语义与 ChaseLev 完全一致，
// 供测试套件交叉验证。
type Mutex[T any] struct {
	mu sync.Mutex
	// data[0] 是 top 端（窃取），data[len-1] 是 bottom 端（所有者）。
	data []*T
}

// NewMutex 创建互斥双端队列，initialCap 为初始切片容量。
func NewMutex[T any](initialCap int) *Mutex[T] {
	if initialCap < 4 {
		initialCap = 4
	}
	return &Mutex[T]{data: make([]*T, 0, initialCap)}
}

func (m *Mutex[T]) PushBottom(v *T) {
	m.mu.Lock()
	m.data = append(m.data, v)
	m.mu.Unlock()
}

func (m *Mutex[T]) PopBottom() (*T, bool) {
	m.mu.Lock()
	n := len(m.data)
	if n == 0 {
		m.mu.Unlock()
		return nil, false
	}
	v := m.data[n-1]
	m.data[n-1] = nil
	m.data = m.data[:n-1]
	m.mu.Unlock()
	return v, true
}

func (m *Mutex[T]) Steal() (*T, bool) {
	m.mu.Lock()
	n := len(m.data)
	if n == 0 {
		m.mu.Unlock()
		return nil, false
	}
	v := m.data[0]
	m.data[0] = nil
	m.data = m.data[1:]
	m.mu.Unlock()
	return v, true
}

func (m *Mutex[T]) Len() int {
	m.mu.Lock()
	n := len(m.data)
	m.mu.Unlock()
	return n
}
