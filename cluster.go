// Package main — cluster.go
//
// N 副本读写仲裁（quorum）模拟的核心。整个“集群”运行在单个进程内：每个
// Replica 是一份独立的内存存储，Cluster 通过方法调用模拟副本间 RPC，并支持
// 故障注入（down / delay）。版本用向量时钟（vector clock）标注，副本为同一
// key 保留多个并发兄弟版本（siblings），读取时做合并与读修复（read repair）。
//
// 重要边界（详见 README）：
//
//	W+R>N 只保证“某次已确认写所用的副本集合”与“某次读所用的副本集合”相交，
//	因而读不会遗漏已经达到写仲裁的版本；它本身并不证明并发写可全局定序，也不
//	自动给出线性一致性（linearizability）。本模拟器保留真实的并发冲突，绝不把
//	未证明的历史强行标记成线性一致。
package main

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Clock 是向量时钟：writer 逻辑分量 -> 该分量的计数。
type Clock map[string]uint64

// Clone 返回深拷贝，避免调用方在持锁之外改动副本内部状态。
func (c Clock) Clone() Clock {
	out := make(Clock, len(c))
	for k, v := range c {
		out[k] = v
	}
	return out
}

// mergeInto 把 other 的分量逐个并入 c（取 max），返回 c 自身。
func (c Clock) mergeInto(other Clock) Clock {
	for k, v := range other {
		if v > c[k] {
			c[k] = v
		}
	}
	return c
}

// cmpClock 比较两个向量时钟：
//
//	-1: a 在 b 之前（b 已知 a 的全部历史，且至少更新一个分量）
//	 0: a 与 b 相等
//	 1: a 在 b 之后
//	 2: a 与 b 并发（互不可比较）
func cmpClock(a, b Clock) int {
	aHasNewer, bHasNewer := false, false
	for k, v := range a {
		if v > b[k] {
			aHasNewer = true
		}
	}
	for k, v := range b {
		if v > a[k] {
			bHasNewer = true
		}
	}
	switch {
	case !aHasNewer && !bHasNewer:
		return 0 // 相等
	case aHasNewer && !bHasNewer:
		return 1 // a 在 b 之后
	case !aHasNewer && bHasNewer:
		return -1 // a 在 b 之前
	default:
		return 2 // 并发
	}
}

// concurrent 判断两个时钟是否并发。
func concurrent(a, b Clock) bool { return cmpClock(a, b) == 2 }

// pruneSiblings 丢弃被支配的旧版本，返回最大版本集合：
// 任意两个返回值的时钟都不构成严格先后关系（要么相等，要么并发）。
func pruneSiblings(vs []Version) []Version {
	maximal := make([]Version, 0, len(vs))
	for _, v := range vs {
		keep := true
		next := maximal[:0:0]
		for _, m := range maximal {
			switch cmpClock(m.Clock, v.Clock) {
			case -1:
				// m 在 v 之前，被 v 支配，丢弃 m。
			case 0:
				// 同一逻辑版本的重复副本：合并已确认标记与确认计数。
				keep = true
				if v.AckCount < m.AckCount {
					v.AckCount = m.AckCount
				}
				if m.Committed {
					v.Committed = true
				}
				// 值以先见到的为准（同分量理应同值）。
			default:
				next = append(next, m)
			}
		}
		maximal = next
		if keep {
			maximal = append(maximal, v)
		}
	}
	return maximal
}

// Version 是某 key 的一个带版本的值（一个 sibling）。
type Version struct {
	ID    string `json:"id"`
	Key   string `json:"key"`
	Value string `json:"value"`
	Clock Clock  `json:"clock"`
	// Committed 表示该版本已由协调者收集到 W 个确认（达到写仲裁）。
	// false 表示它只存在于“部分写成功但超时”的副本上，历史尚未被仲裁确认；
	// 读取到这种版本时接口会明确标记 uncommitted，绝不当作已证明的线性历史。
	Committed bool `json:"committed"`
	// AckCount 是写入协调者已收集到的确认副本数（调试/观测用）。
	AckCount int `json:"ack_count"`
	// Origin 是产生该版本的协调者副本（写入入口），仅用于排查。
	Origin int `json:"origin"`
}

// cloneVersion 深拷贝一个版本（含向量时钟）。
func cloneVersion(v Version) Version {
	v.Clock = v.Clock.Clone()
	return v
}

// mode 是副本的故障模式。
type mode string

const (
	modeUp    mode = "up"
	modeDown  mode = "down"
	modeDelay mode = "delay"
)

// Replica 是一个副本：一把锁保护它的全部内存状态。
type Replica struct {
	id   int
	mu   sync.Mutex
	mode mode
	// delay 是 mode=delay 时响应 RPC 前的人为延迟。
	delay time.Duration
	// data: key -> 该 key 上保留的全部版本（会定期剪枝为最大集）。
	data map[string][]Version
}

func newReplica(id int) *Replica {
	return &Replica{id: id, mode: modeUp, data: make(map[string][]Version)}
}

// setMode 调整故障模式。
func (r *Replica) setMode(m mode, d time.Duration) {
	r.mu.Lock()
	r.mode = m
	if d > 0 {
		r.delay = d
	}
	r.mu.Unlock()
}

func (r *Replica) status() ReplicaStatus {
	r.mu.Lock()
	defer r.mu.Unlock()
	return ReplicaStatus{ID: r.id, Mode: string(r.mode), DelayMS: r.delay.Milliseconds(), Keys: len(r.data)}
}

// apply 把一个版本并入副本存储，剪去被支配的旧版本。必须在持有 r.mu 时调用。
func (r *Replica) apply(v Version) {
	vs := r.data[v.Key]
	for i, ex := range vs {
		switch cmpClock(ex.Clock, v.Clock) {
		case 1:
			// 已存在更新的版本：v 被支配，直接忽略。
			return
		case 0:
			// 同一版本重复投递：合并确认标记/计数，值不变。
			if v.Committed {
				vs[i].Committed = true
			}
			if v.AckCount > vs[i].AckCount {
				vs[i].AckCount = v.AckCount
			}
			r.data[v.Key] = vs
			return
		}
	}
	vs = append(vs, cloneVersion(v))
	r.data[v.Key] = pruneSiblings(vs)
}

// writeIntent 模拟“写入意向”RPC：副本接受新版本。down 立即失败；delay 延迟后成功。
func (r *Replica) writeIntent(v Version) error {
	r.mu.Lock()
	m, d := r.mode, r.delay
	r.mu.Unlock()

	if m == modeDown {
		return errReplicaDown
	}
	if m == modeDelay {
		time.Sleep(d)
		r.mu.Lock()
		// 延迟期间可能被置为 down，以当前模式为准。
		if r.mode == modeDown {
			r.mu.Unlock()
			return errReplicaDown
		}
		r.apply(v)
		r.mu.Unlock()
		return nil
	}
	r.mu.Lock()
	r.apply(v)
	r.mu.Unlock()
	return nil
}

// commit 模拟“提交标记”RPC：把某版本标记为已达到写仲裁。
func (r *Replica) commit(key, id string, ackCount int) error {
	r.mu.Lock()
	if r.mode == modeDown {
		r.mu.Unlock()
		return errReplicaDown
	}
	d := time.Duration(0)
	if r.mode == modeDelay {
		d = r.delay
	}
	if d > 0 {
		// 模拟慢链路：不持锁睡眠。
		r.mu.Unlock()
		time.Sleep(d)
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.mode == modeDown {
			return errReplicaDown
		}
	} else {
		defer r.mu.Unlock()
	}
	for i, v := range r.data[key] {
		if v.ID == id {
			r.data[key][i].Committed = true
			if ackCount > r.data[key][i].AckCount {
				r.data[key][i].AckCount = ackCount
			}
			return nil
		}
	}
	return errVersionNotFound
}

// get 返回某 key 当前保留版本的深拷贝。
func (r *Replica) get(key string) []Version {
	r.mu.Lock()
	defer r.mu.Unlock()
	vs := r.data[key]
	out := make([]Version, len(vs))
	for i, v := range vs {
		out[i] = cloneVersion(v)
	}
	return out
}

// repair 模拟读修复 RPC：把缺失的兄弟版本推给一个副本。
func (r *Replica) repair(vs []Version) error {
	r.mu.Lock()
	m, d := r.mode, r.delay
	r.mu.Unlock()
	if m == modeDown {
		return errReplicaDown
	}
	if m == modeDelay {
		time.Sleep(d)
		r.mu.Lock()
		if r.mode == modeDown {
			r.mu.Unlock()
			return errReplicaDown
		}
	}
	r.mu.Lock()
	for _, v := range vs {
		r.apply(v)
	}
	r.mu.Unlock()
	return nil
}

// keysSnapshot 导出副本全部数据（恢复 / 调试用）的深拷贝。
func (r *Replica) keysSnapshot() map[string][]Version {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string][]Version, len(r.data))
	for k, vs := range r.data {
		cp := make([]Version, len(vs))
		for i, v := range vs {
			cp[i] = cloneVersion(v)
		}
		out[k] = cp
	}
	return out
}

// reset 清空副本数据并恢复为 up。
func (r *Replica) reset() {
	r.mu.Lock()
	r.mode = modeUp
	r.delay = 0
	r.data = make(map[string][]Version)
	r.mu.Unlock()
}

var (
	errReplicaDown     = errors.New("replica is down")
	errVersionNotFound = errors.New("version not found on replica")
)

// commitTimeout 限制写协调后向单个副本传播“已确认”标记的等待时间。
const commitTimeout = 500 * time.Millisecond

// ReplicaStatus 是 /state 中单个副本的状态。
type ReplicaStatus struct {
	ID      int    `json:"id"`
	Mode    string `json:"mode"`
	DelayMS int64  `json:"delay_ms"`
	Keys    int    `json:"keys"`
}

// Cluster 持有全部副本与全局参数 N/W/R。
type Cluster struct {
	mu       sync.Mutex
	n, w, r  int
	replicas []*Replica
	writeSeq int
}

// NewCluster 创建 n 个副本的集群，初始仲裁为 w/r。
func NewCluster(n, w, r int) (*Cluster, error) {
	if n < 1 {
		return nil, errors.New("N must be >= 1")
	}
	if err := validateQuorum(n, w, r); err != nil {
		return nil, err
	}
	c := &Cluster{n: n, w: w, r: r, replicas: make([]*Replica, n)}
	for i := 0; i < n; i++ {
		c.replicas[i] = newReplica(i)
	}
	return c, nil
}

func validateQuorum(n, w, r int) error {
	if w < 1 || w > n || r < 1 || r > n {
		return fmt.Errorf("W and R must be in [1,N=%d] (got W=%d R=%d)", n, w, r)
	}
	return nil
}

// Config 当前参数。
func (c *Cluster) Config() (n, w, r int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.n, c.w, c.r
}

// SetConfig 更新 W/R；N 变化会重建并清空全部副本（模拟器不实现成员变更）。
func (c *Cluster) SetConfig(n, w, r int) (reset bool, err error) {
	if err := validateQuorum(n, w, r); err != nil {
		return false, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if n != c.n {
		c.n, c.w, c.r = n, w, r
		c.replicas = make([]*Replica, n)
		for i := 0; i < n; i++ {
			c.replicas[i] = newReplica(i)
		}
		return true, nil
	}
	c.w, c.r = w, r
	return false, nil
}

// replica 取出副本，id 越界时返回错误。
func (c *Cluster) replica(id int) (*Replica, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if id < 0 || id >= len(c.replicas) {
		return nil, fmt.Errorf("replica id %d out of range [0,%d)", id, c.n)
	}
	return c.replicas[id], nil
}

// SetFault 故障注入。
func (c *Cluster) SetFault(id int, m mode, delay time.Duration) error {
	rep, err := c.replica(id)
	if err != nil {
		return err
	}
	rep.setMode(m, delay)
	return nil
}

// Status 全部副本状态。
func (c *Cluster) Status() []ReplicaStatus {
	c.mu.Lock()
	reps := append([]*Replica(nil), c.replicas...)
	c.mu.Unlock()
	out := make([]ReplicaStatus, len(reps))
	for i, rep := range reps {
		out[i] = rep.status()
	}
	return out
}

// Snapshot 全部副本数据（/state 调试用）。
func (c *Cluster) Snapshot() []ReplicaSnapshot {
	c.mu.Lock()
	reps := append([]*Replica(nil), c.replicas...)
	c.mu.Unlock()
	out := make([]ReplicaSnapshot, len(reps))
	for i, rep := range reps {
		st := rep.status()
		out[i] = ReplicaSnapshot{ReplicaStatus: st, Data: rep.keysSnapshot()}
	}
	return out
}

// ReplicaSnapshot 是 /state 返回的单副本完整快照。
type ReplicaSnapshot struct {
	ReplicaStatus
	Data map[string][]Version `json:"data"`
}

// Reset 清空所有数据与故障注入。
func (c *Cluster) Reset() {
	c.mu.Lock()
	reps := append([]*Replica(nil), c.replicas...)
	c.writeSeq = 0
	c.mu.Unlock()
	for _, rep := range reps {
		rep.reset()
	}
}

// WriteOptions 控制一次写入的接触副本集合与超时。
type WriteOptions struct {
	W       int
	Targets []int // nil 表示全部 N 个副本
	Timeout time.Duration
}

// ReplicaResult 是单个副本的 RPC 结果。
type ReplicaResult struct {
	Replica int    `json:"replica"`
	OK      bool   `json:"ok"`
	Status  string `json:"status"` // acked | pending | timeout | error
	Error   string `json:"error,omitempty"`
}

// WriteOutcome 是协调者一次写入的完整结果。
type WriteOutcome struct {
	WriteID     string          `json:"write_id"`
	Key         string          `json:"key"`
	Value       string          `json:"value"`
	Clock       Clock           `json:"clock"`
	W           int             `json:"w"`
	Acked       int             `json:"acked"`
	Quorum      bool            `json:"quorum"`
	Committed   bool            `json:"committed"`
	Timeout     bool            `json:"timeout"`
	Results     []ReplicaResult `json:"results"`
	Consistency string          `json:"consistency_note"`
}

// Write 执行一次写入协调：
//  1. 依据 context（因果向量时钟）推进本次写的逻辑分量，得到新版本；
//  2. 并行向目标副本发送意向；
//  3. 在 timeout 内收到 W 个确认即成功，并尽力把“已确认”标记传播给所有副本；
//  4. 超时未达 W 则整体失败——但已经写入的副本上会留下 uncommitted 版本，
//     这正是“部分写成功后超时”的场景，读取时会被如实标出。
func (c *Cluster) Write(key, value string, context Clock, coord int, opts WriteOptions) WriteOutcome {
	c.mu.Lock()
	n, w := c.n, c.w
	if opts.W > 0 {
		w = opts.W
	}
	if coord < 0 || coord >= n {
		coord = 0
	}
	c.writeSeq++
	seq := c.writeSeq
	targets := opts.Targets
	if targets == nil {
		targets = make([]int, n)
		for i := range targets {
			targets[i] = i
		}
	}
	reps := append([]*Replica(nil), c.replicas...)
	c.mu.Unlock()

	writer := fmt.Sprintf("w%d", seq)
	clock := context.Clone()
	clock[writer]++
	version := Version{
		ID:     fmt.Sprintf("write-%d", seq),
		Key:    key,
		Value:  value,
		Clock:  clock,
		Origin: coord,
	}

	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = 2 * time.Second
	}

	type resp struct {
		id  int
		err error
	}
	resCh := make(chan resp, len(targets))
	for _, id := range targets {
		go func(id int) {
			err := reps[id].writeIntent(cloneVersion(version))
			resCh <- resp{id: id, err: err}
		}(id)
	}

	results := make(map[int]*ReplicaResult, len(targets))
	for _, id := range targets {
		results[id] = &ReplicaResult{Replica: id, Status: "pending"}
	}
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	acked := 0
	timedOut := false
	received := 0
	for received < len(targets) {
		select {
		case r := <-resCh:
			received++
			rr := results[r.id]
			if r.err != nil {
				rr.OK = false
				if errors.Is(r.err, errReplicaDown) {
					rr.Status = "error"
					rr.Error = "replica down"
				} else {
					rr.Status = "error"
					rr.Error = r.err.Error()
				}
			} else {
				rr.OK = true
				rr.Status = "acked"
				acked++
			}
		case <-deadline.C:
			timedOut = true
			// 协调者等待上限：慢副本的在途意向会继续落地，但不计入本次结果。
			received = len(targets)
		}
	}
	// 成功判据是 W 个确认；慢/失败副本不影响已经凑齐的仲裁。
	quorum := acked >= w

	// 仍在途（delay）的意向 RPC 会在各自 goroutine 中继续执行并落地副本；
	// resCh 按目标数做了全缓冲，迟到的回复不会阻塞这些 goroutine。
	// 它们的实际效果（晚到的 uncommitted 版本）可通过读/状态看到。

	// 超时路径上仍 pending 的标记为 timeout。
	if timedOut {
		for _, rr := range results {
			if rr.Status == "pending" {
				rr.Status = "timeout"
				rr.Error = "reply after coordinator timeout"
			}
		}
	}

	// 达到仲裁：尽力把 committed 标记同步传播给所有已确认副本（best-effort，
	// 单个副本的提交只等 commitTimeout；慢副本上的标记可随后由读修复补齐）。
	// 注意：不能在这里改写被在途意向 goroutine 读取的 version。
	if quorum {
		for _, id := range targets {
			rr := results[id]
			if rr.Status != "acked" {
				continue
			}
			rep := reps[id]
			done := make(chan struct{}, 1)
			go func() {
				_ = rep.commit(key, version.ID, acked)
				done <- struct{}{}
			}()
			select {
			case <-done:
			case <-time.After(commitTimeout):
			}
		}
	}

	out := WriteOutcome{
		WriteID:     version.ID,
		Key:         key,
		Value:       value,
		Clock:       clock,
		W:           w,
		Acked:       acked,
		Quorum:      quorum,
		Committed:   quorum,
		Timeout:     timedOut && !quorum,
		Results:     flattenResults(results, targets),
		Consistency: consistencyNote,
	}
	return out
}

func flattenResults(m map[int]*ReplicaResult, order []int) []ReplicaResult {
	out := make([]ReplicaResult, 0, len(order))
	for _, id := range order {
		if rr, ok := m[id]; ok {
			out = append(out, *rr)
		}
	}
	// 顺序稳定，便于测试与演示比对。
	sort.SliceStable(out, func(i, j int) bool { return out[i].Replica < out[j].Replica })
	return out
}

const consistencyNote = "W+R>N only proves the read and quorum-write replica sets intersect; " +
	"it does NOT establish a global order of concurrent writes and does NOT by itself guarantee linearizability."

// ReadOptions 控制读集合、超时与读修复。
type ReadOptions struct {
	R             int
	Targets       []int // nil 表示全部 N 个副本
	Timeout       time.Duration
	Repair        bool
	RepairFull    bool // true：向全部 up 副本修复；false：只向本次响应副本修复
	RepairTimeout time.Duration
}

// ReadOutcome 是协调者一次读取的完整结果。
type ReadOutcome struct {
	Key            string          `json:"key"`
	R              int             `json:"r"`
	Responses      int             `json:"responses"`
	Quorum         bool            `json:"quorum"`
	Found          bool            `json:"found"`
	Conflict       bool            `json:"conflict"`
	Value          string          `json:"value,omitempty"`    // 单版本时给出
	Versions       []Version       `json:"versions,omitempty"` // 合并后的最大版本集
	HasUncommitted bool            `json:"has_uncommitted"`
	RepairedTo     []int           `json:"repaired_to,omitempty"`
	Results        []ReplicaResult `json:"results"`
	Consistency    string          `json:"consistency_note"`
}

// Read 执行一次读协调：并行读 R（或指定目标）个副本，合并兄弟版本，
// 检测并发冲突与未确认版本，并按需要做读修复。
func (c *Cluster) Read(key string, opts ReadOptions) ReadOutcome {
	c.mu.Lock()
	n, r := c.n, c.r
	if opts.R > 0 {
		r = opts.R
	}
	targets := opts.Targets
	if targets == nil {
		targets = make([]int, n)
		for i := range targets {
			targets[i] = i
		}
	}
	reps := append([]*Replica(nil), c.replicas...)
	c.mu.Unlock()

	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = 2 * time.Second
	}

	type resp struct {
		id  int
		vs  []Version
		err error
	}
	resCh := make(chan resp, len(targets))
	for _, id := range targets {
		go func(id int) {
			rep := reps[id]
			rep.mu.Lock()
			m, d := rep.mode, rep.delay
			rep.mu.Unlock()
			if m == modeDown {
				resCh <- resp{id: id, err: errReplicaDown}
				return
			}
			if m == modeDelay {
				time.Sleep(d)
				rep.mu.Lock()
				if rep.mode == modeDown {
					rep.mu.Unlock()
					resCh <- resp{id: id, err: errReplicaDown}
					return
				}
				rep.mu.Unlock()
			}
			resCh <- resp{id: id, vs: reps[id].get(key)}
		}(id)
	}

	resultByID := make(map[int]*ReplicaResult, len(targets))
	for _, id := range targets {
		resultByID[id] = &ReplicaResult{Replica: id, Status: "pending"}
	}
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	merged := []Version{}
	responses := 0
	timedOut := false
	for received := 0; received < len(targets); {
		select {
		case x := <-resCh:
			received++
			rr := resultByID[x.id]
			if x.err != nil {
				rr.Status = "error"
				rr.OK = false
				if errors.Is(x.err, errReplicaDown) {
					rr.Error = "replica down"
				} else {
					rr.Error = x.err.Error()
				}
			} else {
				rr.Status = "acked"
				rr.OK = true
				responses++
				merged = append(merged, x.vs...)
			}
		case <-deadline.C:
			timedOut = true
			// 协调者等待上限：慢副本的在途读会自行结束，回复写入全缓冲 channel。
			received = len(targets)
		}
	}
	if timedOut {
		for _, rr := range resultByID {
			if rr.Status == "pending" {
				rr.Status = "timeout"
				rr.Error = "reply after coordinator timeout"
			}
		}
	}

	merged = pruneSiblings(merged)
	quorum := responses >= r
	hasUncommitted := false
	for _, v := range merged {
		if !v.Committed {
			hasUncommitted = true
			break
		}
	}

	out := ReadOutcome{
		Key:            key,
		R:              r,
		Responses:      responses,
		Quorum:         quorum,
		Found:          len(merged) > 0,
		Conflict:       len(merged) > 1,
		Versions:       merged,
		HasUncommitted: hasUncommitted,
		Results:        flattenResults(resultByID, targets),
		Consistency:    consistencyNote,
	}
	if len(merged) == 1 {
		out.Value = merged[0].Value
	}

	// 读修复：即使没凑齐 R（比如 R=N 且有副本 down），也对响应过的副本做修复；
	// 是否修复以及修复范围由选项控制。down 副本自然跳过（恢复时走 /recover）。
	if opts.Repair && len(merged) > 0 {
		repairTargets := []int{}
		if opts.RepairFull {
			for _, rep := range reps {
				st := rep.status()
				if st.Mode == string(modeUp) {
					repairTargets = append(repairTargets, st.ID)
				}
			}
		} else {
			for _, rr := range out.Results {
				if rr.Status == "acked" {
					repairTargets = append(repairTargets, rr.Replica)
				}
			}
		}
		repairTimeout := opts.RepairTimeout
		if repairTimeout <= 0 {
			repairTimeout = 1500 * time.Millisecond
		}
		for _, id := range repairTargets {
			current := reps[id].get(key)
			need := missingForRepair(current, merged)
			if len(need) == 0 {
				continue
			}
			done := make(chan error, 1)
			go func(id int, need []Version) {
				done <- reps[id].repair(need)
			}(id, need)
			select {
			case err := <-done:
				if err == nil {
					out.RepairedTo = append(out.RepairedTo, id)
				}
			case <-time.After(repairTimeout):
				// 慢副本上的修复在后台继续，不计入本次修复结果。
			}
		}
		sort.Ints(out.RepairedTo)
	}

	return out
}

// missingForRepair 计算目标副本相对最大集还缺哪些版本（已有的不重发）。
func missingForRepair(current, maximal []Version) []Version {
	need := []Version{}
	for _, want := range maximal {
		have := false
		for _, c := range current {
			switch cmpClock(c.Clock, want.Clock) {
			case 0:
				have = true
				// 若本地副本已有同版本但 committed 标记落后，仍需补传以更新标记。
				if !c.Committed && want.Committed {
					have = false
				}
			case 1:
				// 本地已有更新的支配版本，无需修复该 want。
				have = true
			}
		}
		if !have {
			need = append(need, want)
		}
	}
	return need
}

// RecoverOptions 控制副本恢复（反熵）过程。
type RecoverOptions struct {
	Target  int
	From    []int // nil 表示从所有 up 副本拉取
	Timeout time.Duration
}

// RecoverOutcome 是恢复结果。
type RecoverOutcome struct {
	Target       int             `json:"target"`
	FromReplicas []int           `json:"from_replicas"`
	KeysPulled   map[string]int  `json:"keys_pulled"` // key -> 并入的版本数
	Status       string          `json:"status"`      // recovered | error
	Error        string          `json:"error,omitempty"`
	Results      []ReplicaResult `json:"results"`
	Consistency  string          `json:"consistency_note"`
}

// Recover 把一个 down 副本重新置为 up，并从其余健康副本拉取全部 key 的最大
// 版本集合完成反熵（模拟副本恢复后的同步）。
func (c *Cluster) Recover(opts RecoverOptions) RecoverOutcome {
	c.mu.Lock()
	n := c.n
	if opts.Target < 0 || opts.Target >= n {
		c.mu.Unlock()
		return RecoverOutcome{Status: "error", Error: fmt.Sprintf("target %d out of range", opts.Target), Consistency: consistencyNote}
	}
	target := c.replicas[opts.Target]
	srcIDs := opts.From
	if srcIDs == nil {
		for i := 0; i < n; i++ {
			if i != opts.Target {
				srcIDs = append(srcIDs, i)
			}
		}
	}
	srcs := make([]*Replica, len(srcIDs))
	for i, id := range srcIDs {
		srcs[i] = c.replicas[id]
	}
	c.mu.Unlock()

	// 恢复：先回到 up，再合并数据。
	target.setMode(modeUp, 0)
	keysPulled := map[string]int{}
	results := make([]ReplicaResult, 0, len(srcs))

	// 收集每个源副本的全量快照并按 key 合并。
	byKey := map[string][]Version{}
	for i, src := range srcs {
		snap := src.keysSnapshot()
		ok := true
		status := "acked"
		errMsg := ""
		if src.status().Mode == string(modeDown) {
			ok = false
			status = "error"
			errMsg = "source replica down"
		}
		results = append(results, ReplicaResult{Replica: srcIDs[i], OK: ok, Status: status, Error: errMsg})
		if !ok {
			continue
		}
		for k, vs := range snap {
			byKey[k] = append(byKey[k], vs...)
		}
	}
	for k, vs := range byKey {
		maximal := pruneSiblings(vs)
		before := len(target.get(k))
		target.mu.Lock()
		for _, v := range maximal {
			target.apply(v)
		}
		target.mu.Unlock()
		after := len(target.get(k))
		if after > before {
			keysPulled[k] = after - before
		}
	}
	sort.SliceStable(results, func(i, j int) bool { return results[i].Replica < results[j].Replica })
	return RecoverOutcome{
		Target:       opts.Target,
		FromReplicas: srcIDs,
		KeysPulled:   keysPulled,
		Status:       "recovered",
		Results:      results,
		Consistency:  consistencyNote,
	}
}
