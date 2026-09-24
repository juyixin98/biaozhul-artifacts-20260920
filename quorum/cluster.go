package quorum

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Version 是一个带版本（向量钟）的值。同一 key 下可能同时存在多个
// 互不可比较的 Version，即兄弟版本（siblings / 冲突）。
type Version struct {
	ID        string `json:"id"`
	Value     string `json:"value"`
	Clock     Clock  `json:"clock"`
	WrittenAt string `json:"written_at"` // 仅用于展示；冲突判定不依赖物理时间
}

// Replica 是一个内存副本：key -> 该副本持有的全部版本。
// 版本不会被就地覆盖：新写追加版本，被支配（dominated）的旧版本
// 在合并时被剪枝，但副本保留其已知历史，便于演示与观察。
type Replica struct {
	mu   sync.Mutex
	data map[string][]Version
}

func newReplica() *Replica {
	return &Replica{data: map[string][]Version{}}
}

// applyVersion 把一个版本并入副本；同 ID 或同向量钟不重复保存。
func (r *Replica) applyVersion(key string, v Version) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, existing := range r.data[key] {
		if existing.ID == v.ID || ClockEqual(existing.Clock, v.Clock) {
			return
		}
	}
	r.data[key] = append(r.data[key], v)
}

func (r *Replica) versions(key string) []Version {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Version, len(r.data[key]))
	copy(out, r.data[key])
	return out
}

// snapshot 返回 key -> 版本列表 的拷贝（状态观察用）。
func (r *Replica) snapshot() map[string][]Version {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string][]Version, len(r.data))
	for k, vs := range r.data {
		cp := make([]Version, len(vs))
		copy(cp, vs)
		out[k] = cp
	}
	return out
}

func (r *Replica) reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.data = map[string][]Version{}
}

// Config 是集群的 N/W/R 配置。W、R 必须在 [1,N] 区间。
type Config struct {
	N int `json:"n"`
	W int `json:"w"`
	R int `json:"r"`
}

// DefaultTiming 是模拟网络的默认时间参数。
type DefaultTiming struct {
	// RpcDelay 是一次成功到达副本的“网络往返”延迟。
	RpcDelay time.Duration
	// Deadline 是协调者等待 W/R 个响应的最长时间。
	// 宕机副本永不响应，因此 deadline 到期即表现为“部分写成功后超时”。
	Deadline time.Duration
}

// Cluster 持有 N 个副本、配置、故障集合与模拟时钟。
type Cluster struct {
	mu sync.Mutex

	cfg     Config
	replica map[string]*Replica
	order   []string // n1..nN，稳定顺序
	down    map[string]bool

	// counters：每个节点逻辑 ID 对应的向量钟自增计数。
	counters map[string]int
	writes   int // 集群级写序号，用于生成可读的版本 ID

	timing DefaultTiming
	now    func() time.Time // 可在测试中替换
}

// Option 用于构造 Cluster。
type Option func(*Cluster)

// WithTiming 覆盖默认模拟时间参数。
func WithTiming(rpcDelay, deadline time.Duration) Option {
	return func(c *Cluster) {
		c.timing = DefaultTiming{RpcDelay: rpcDelay, Deadline: deadline}
	}
}

// WithClock 注入时间源（测试用）。
func WithClock(f func() time.Time) Option {
	return func(c *Cluster) { c.now = f }
}

// New 创建一个 N 副本集群，节点逻辑 ID 为 n1..nN。
func New(cfg Config, opts ...Option) (*Cluster, error) {
	if cfg.N < 1 {
		return nil, errors.New("N 必须 >= 1")
	}
	if cfg.W < 1 || cfg.W > cfg.N {
		return nil, fmt.Errorf("W=%d 越界，必须在 [1,%d]", cfg.W, cfg.N)
	}
	if cfg.R < 1 || cfg.R > cfg.N {
		return nil, fmt.Errorf("R=%d 越界，必须在 [1,%d]", cfg.R, cfg.N)
	}
	c := &Cluster{
		cfg:      cfg,
		replica:  map[string]*Replica{},
		down:     map[string]bool{},
		counters: map[string]int{},
		timing:   DefaultTiming{RpcDelay: 10 * time.Millisecond, Deadline: 60 * time.Millisecond},
		now:      time.Now,
	}
	for _, o := range opts {
		o(c)
	}
	for i := 1; i <= cfg.N; i++ {
		id := fmt.Sprintf("n%d", i)
		c.replica[id] = newReplica()
		c.order = append(c.order, id)
	}
	return c, nil
}

// Config 返回当前配置的拷贝。
func (c *Cluster) Config() Config {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cfg
}

// Reconfigure 在 [1,N] 范围内修改 W/R。用于演示 W+R<=N 的场景。
func (c *Cluster) Reconfigure(w, r int) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if w < 1 || w > c.cfg.N || r < 1 || r > c.cfg.N {
		return fmt.Errorf("W/R 必须在 [1,%d]", c.cfg.N)
	}
	c.cfg.W, c.cfg.R = w, r
	return nil
}

// Nodes 返回副本 ID 的稳定顺序。
func (c *Cluster) Nodes() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, len(c.order))
	copy(out, c.order)
	return out
}

// SetDown / SetUp 注入/解除副本故障。宕机副本不响应任何 RPC（超时），
// 其上的数据仍然保留，SetUp 后带着旧数据回到集群——这正是
// “部分写 + 副本恢复”场景的基础。
func (c *Cluster) SetDown(id string) error { return c.setDown(id, true) }
func (c *Cluster) SetUp(id string) error   { return c.setDown(id, false) }

func (c *Cluster) setDown(id string, down bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.replica[id]; !ok {
		return fmt.Errorf("未知副本 %q", id)
	}
	if down {
		c.down[id] = true
	} else {
		delete(c.down, id)
	}
	return nil
}

// DownSet 返回当前宕机副本的排序列表。
func (c *Cluster) DownSet() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.downSetLocked()
}

func (c *Cluster) downSetLocked() []string {
	out := make([]string, 0, len(c.down))
	for id := range c.down {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// resolveTargets 把调用方给定的子集（空表示全部副本，宕机者仍列出）
// 展开为有序 ID 列表。宕机节点保留在目标集合中，由 fanout 模拟丢包，
// 这样响应里能如实统计 timed_out_by。
func (c *Cluster) resolveTargets(nodes []string) ([]string, error) {
	if len(nodes) == 0 {
		out := make([]string, len(c.order))
		copy(out, c.order)
		return out, nil
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(nodes))
	for _, id := range nodes {
		if _, ok := c.replica[id]; !ok {
			return nil, fmt.Errorf("未知副本 %q", id)
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	return out, nil
}

// rpcResult 是一次副本 RPC 的结果。
type rpcResult struct {
	node     string
	versions []Version // 读：副本上的版本；写：写入后的该 key 全量集合
	ok       bool
}

// fanout 向 targets 并发发送 RPC。宕机节点直接不响应（无结果，模拟丢包）；
// 在线节点延迟 rpcDelay 后执行 work 并回包。返回结果通道与 WaitGroup：
// Wait() 可等待所有在线 RPC 落地（用于精确统计“迟到确认”）。
func (c *Cluster) fanout(targets []string, work func(node string) []Version) (<-chan rpcResult, *sync.WaitGroup) {
	ch := make(chan rpcResult, len(targets))
	var wg sync.WaitGroup
	for _, id := range targets {
		id := id
		c.mu.Lock()
		down := c.down[id]
		delay := c.timing.RpcDelay
		c.mu.Unlock()
		if down {
			continue // 宕机：请求丢失，永不回包
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			// 模拟往返延迟。协调者可能在延迟期间就因达到 W 而提前返回，
			// 甚至可能在 deadline 后该写才落地——这正是“部分写”的来源：
			// 超时只是协调者不再等待，请求没有事务回滚。
			time.Sleep(delay)
			vs := work(id)
			ch <- rpcResult{node: id, ok: true, versions: vs}
		}()
	}
	return ch, &wg
}

// WriteRequest 是一次仲裁写的参数。
type WriteRequest struct {
	Key         string   `json:"key"`
	Value       string   `json:"value"`
	Coordinator string   `json:"coordinator"` // 向量钟自增所用的协调者逻辑 ID
	Parents     []Clock  `json:"-"`           // 来自 API 层解析后的父钟
	Nodes       []string `json:"nodes"`       // 可选：限制接触的副本子集
	DeadlineMS  int      `json:"deadline_ms"` // 可选：覆盖默认 deadline
}

// WriteResponse 是仲裁写的真实结果——确认与超时副本都会如实列出。
type WriteResponse struct {
	Key        string   `json:"key"`
	Version    Version  `json:"version"`
	QuorumW    int      `json:"w"`
	AckedBy    []string `json:"acked_by"`
	TimedOutBy []string `json:"timed_out_by"`
	Contacted  []string `json:"contacted"`
	QuorumMet  bool     `json:"quorum_met"`
	ElapsedMS  int64    `json:"elapsed_ms"`
	Note       string   `json:"note,omitempty"`
}

// Write 执行一次 W 仲裁写。
//
// 新钟 = max(所有父钟)，并在协调者分量上 +1（Dynamo 风格）。
// 协调者在 W 个确认到达时即视为成功（法定人数写的标准行为）；
// 若 deadline 到期仍不足 W，则返回 QuorumMet=false（HTTP 504），
// 但已经落地（含 deadline 前后窗口内落地）的副本写不会回滚——
// 调用方可以从响应里看到真实部分写。
func (c *Cluster) Write(req WriteRequest) (WriteResponse, error) {
	if req.Key == "" {
		return WriteResponse{}, errors.New("key 不能为空")
	}
	if req.Value == "" {
		return WriteResponse{}, errors.New("value 不能为空")
	}
	c.mu.Lock()
	coord := req.Coordinator
	if coord == "" {
		coord = c.order[0]
	}
	if _, ok := c.replica[coord]; !ok {
		c.mu.Unlock()
		return WriteResponse{}, fmt.Errorf("未知协调者 %q", coord)
	}
	targets, err := c.resolveTargets(req.Nodes)
	if err != nil {
		c.mu.Unlock()
		return WriteResponse{}, err
	}
	merged := MergeClones(req.Parents...)
	next := c.counters[coord]
	if base := merged[coord]; base > next {
		next = base // 父钟携带同协调者更高计数（带上下文写）
	}
	next++
	c.counters[coord] = next
	clock := merged.Clone()
	clock[coord] = next
	c.writes++
	ver := Version{
		ID:        newVersionID(c.writes, coord),
		Value:     req.Value,
		Clock:     clock,
		WrittenAt: c.now().UTC().Format(time.RFC3339Nano),
	}
	deadline := c.timing.Deadline
	if req.DeadlineMS > 0 {
		deadline = time.Duration(req.DeadlineMS) * time.Millisecond
	}
	w := c.cfg.W
	c.mu.Unlock()

	start := time.Now()
	ch, wg := c.fanout(targets, func(node string) []Version {
		c.replica[node].applyVersion(req.Key, ver)
		return c.replica[node].versions(req.Key)
	})

	acked := map[string]bool{}
	timer := time.NewTimer(deadline)
	met := false
collect:
	for {
		select {
		case r := <-ch:
			if r.ok {
				acked[r.node] = true
			}
			if len(acked) >= w {
				met = true
				break collect
			}
		case <-timer.C:
			break collect
		}
	}
	elapsed := time.Since(start)

	// 等待所有在线 RPC 结束（至多再等一个 rpcDelay），让 acked/timed_out
	// 如实反映“最终哪些副本真正收到了写”。注意：协调者的成功/超时决定
	// 只取决于上面的 W 与 deadline，不受这里影响。
	wg.Wait()
drain:
	for {
		select {
		case r := <-ch:
			if r.ok {
				acked[r.node] = true
			}
		default:
			break drain
		}
	}
	timedOut := map[string]bool{}
	for _, id := range targets {
		if !acked[id] {
			timedOut[id] = true
		}
	}

	resp := WriteResponse{
		Key:        req.Key,
		Version:    ver,
		QuorumW:    w,
		AckedBy:    sortedKeys(acked),
		TimedOutBy: sortedKeys(timedOut),
		Contacted:  append([]string{}, targets...),
		QuorumMet:  met,
		ElapsedMS:  elapsed.Milliseconds(),
	}
	switch {
	case !met:
		resp.Note = "写未达到 W 法定人数：部分副本已持久化但整体写超时；数据不会回滚，之后可能被读修复或作为冲突暴露。"
	case len(timedOut) > 0:
		resp.Note = "写在 W 个确认后即成功返回；timed_out_by 中的副本未参与本次写，恢复后持有旧数据，等待读修复/反熵。"
	}
	return resp, nil
}

// ReadRequest 是一次仲裁读的参数。
type ReadRequest struct {
	Key        string   `json:"key"`
	Nodes      []string `json:"nodes"`
	NoRepair   bool     `json:"no_repair"` // 仅读不修复
	DeadlineMS int      `json:"deadline_ms"`
}

// ReadResponse 是仲裁读的结果。
type ReadResponse struct {
	Key         string    `json:"key"`
	QuorumR     int       `json:"r"`
	RespondedBy []string  `json:"responded_by"`
	TimedOutBy  []string  `json:"timed_out_by"`
	Contacted   []string  `json:"contacted"`
	QuorumMet   bool      `json:"quorum_met"`
	Versions    []Version `json:"versions"` // 合并剪枝后的最大版本集
	Conflict    bool      `json:"conflict"`
	RepairedTo  []string  `json:"repaired_to,omitempty"`
	NotFound    bool      `json:"not_found"`
	ElapsedMS   int64     `json:"elapsed_ms"`
	Note        string    `json:"note,omitempty"`
}

// Read 执行一次 R 仲裁读，合并响应、剪枝被支配版本，并对所有
// 可达副本做（激进策略的）读修复。
func (c *Cluster) Read(req ReadRequest) (ReadResponse, error) {
	if req.Key == "" {
		return ReadResponse{}, errors.New("key 不能为空")
	}
	c.mu.Lock()
	targets, err := c.resolveTargets(req.Nodes)
	if err != nil {
		c.mu.Unlock()
		return ReadResponse{}, err
	}
	deadline := c.timing.Deadline
	if req.DeadlineMS > 0 {
		deadline = time.Duration(req.DeadlineMS) * time.Millisecond
	}
	r := c.cfg.R
	c.mu.Unlock()

	start := time.Now()
	ch, wg := c.fanout(targets, func(node string) []Version {
		return c.replica[node].versions(req.Key)
	})

	responded := map[string][]Version{}
	timer := time.NewTimer(deadline)
	met := false
collect:
	for {
		select {
		case res := <-ch:
			if res.ok {
				responded[res.node] = res.versions
			}
			if len(responded) >= r {
				met = true
				break collect
			}
		case <-timer.C:
			break collect
		}
	}
	elapsed := time.Since(start)

	wg.Wait()
drain:
	for {
		select {
		case res := <-ch:
			if res.ok {
				responded[res.node] = res.versions
			}
		default:
			break drain
		}
	}

	resp := ReadResponse{
		Key:       req.Key,
		QuorumR:   r,
		Contacted: append([]string{}, targets...),
	}
	sort.Strings(resp.Contacted)
	resp.RespondedBy = sortedKeysBool(responded)
	for _, id := range targets {
		if _, ok := responded[id]; !ok {
			resp.TimedOutBy = append(resp.TimedOutBy, id)
		}
	}
	resp.QuorumMet = met

	var all []Version
	for _, vs := range responded {
		all = append(all, vs...)
	}
	merged := maximalVersions(all)
	resp.Versions = merged
	resp.Conflict = len(merged) > 1
	resp.ElapsedMS = elapsed.Milliseconds()
	resp.NotFound = len(merged) == 0

	// 读修复：把合并出的全量最大版本集写到所有可达但版本落后的副本。
	// 真实 Dynamo 默认只做“部分读修复 + 反熵（Merkle）兜底”，
	// 本模拟器默认对所有可达副本激进修复，以便清楚演示修复路径。
	if met && !req.NoRepair && len(merged) > 0 {
		repaired := map[string]bool{}
		for _, id := range targets {
			c.mu.Lock()
			down := c.down[id]
			c.mu.Unlock()
			if down {
				continue
			}
			before := maximalVersions(c.replica[id].versions(req.Key))
			if versionSetEqual(before, merged) {
				continue
			}
			for _, v := range merged {
				c.replica[id].applyVersion(req.Key, v)
			}
			repaired[id] = true
		}
		resp.RepairedTo = sortedKeys(repaired)
	}

	switch {
	case !met:
		resp.Note = "读未达到 R 法定人数：返回的是部分响应，不能据此判断最新值，也不做读修复。"
	case resp.NotFound:
		resp.Note = "法定人数内没有任何副本持有该 key。"
	case resp.Conflict:
		resp.Note = "存在并发（向量钟不可比较）的兄弟版本。W+R>N 无法自动消除并发写冲突；这些历史不构成线性一致，需要客户端解决（见 /resolve）。"
	case len(resp.RepairedTo) > 0:
		resp.Note = "读到唯一最新版本；已对版本落后的可达副本执行读修复。"
	}
	return resp, nil
}

// ResolveRequest 用客户端裁决后的值消解兄弟版本。
type ResolveRequest struct {
	Key         string   `json:"key"`
	Value       string   `json:"value"`
	Coordinator string   `json:"coordinator"`
	Clocks      []Clock  `json:"-"`
	Nodes       []string `json:"nodes"`
	DeadlineMS  int      `json:"deadline_ms"`
}

// Resolve 以“合并所有兄弟钟并在协调者分量 +1”的方式写入一个
// 后继版本（causal reconciliation）。新版本在因果上后于所有被消解
// 的兄弟版本，之后的读将只返回它（旧兄弟保留在副本历史中）。
func (c *Cluster) Resolve(req ResolveRequest) (WriteResponse, error) {
	coord := req.Coordinator
	if coord == "" {
		coord = c.Nodes()[0]
	}
	return c.Write(WriteRequest{
		Key:         req.Key,
		Value:       req.Value,
		Coordinator: coord,
		Parents:     req.Clocks,
		Nodes:       req.Nodes,
		DeadlineMS:  req.DeadlineMS,
	})
}

// RepairRequest 是手工反熵修复（副本恢复）的参数。
type RepairRequest struct {
	Key   string   `json:"key"`
	Nodes []string `json:"nodes"` // 缺省=所有可达副本
}

// RepairResponse 描述一次反熵修复结果。
type RepairResponse struct {
	Key      string    `json:"key"`
	Versions []Version `json:"versions"`
	Updated  []string  `json:"updated"`
}

// AntiEntropy 用当前在线副本的最大版本集主动修复目标副本集合
// （模拟副本恢复后、或运维触发的反熵过程）。
func (c *Cluster) AntiEntropy(req RepairRequest) (RepairResponse, error) {
	if req.Key == "" {
		return RepairResponse{}, errors.New("key 不能为空")
	}
	c.mu.Lock()
	targets, err := c.resolveTargets(req.Nodes)
	c.mu.Unlock()
	if err != nil {
		return RepairResponse{}, err
	}

	var union []Version
	for _, id := range targets {
		union = append(union, c.replica[id].versions(req.Key)...)
	}
	best := maximalVersions(union)
	updated := map[string]bool{}
	for _, id := range targets {
		c.mu.Lock()
		down := c.down[id]
		c.mu.Unlock()
		if down {
			continue
		}
		cur := maximalVersions(c.replica[id].versions(req.Key))
		if versionSetEqual(cur, best) {
			continue
		}
		for _, v := range best {
			c.replica[id].applyVersion(req.Key, v)
		}
		updated[id] = true
	}
	return RepairResponse{Key: req.Key, Versions: best, Updated: sortedKeys(updated)}, nil
}

// StateResponse 是整个集群或单个 key 的内部状态快照（验收/调试用）。
type StateResponse struct {
	Config   Config                          `json:"config"`
	Down     []string                        `json:"down"`
	Replicas map[string]map[string][]Version `json:"replicas"`
}

// State 返回内部状态。key 为空时返回每个副本的全部 key；
// 指定 key 时只返回该 key。
func (c *Cluster) State(key string) StateResponse {
	c.mu.Lock()
	defer c.mu.Unlock()
	resp := StateResponse{
		Config:   c.cfg,
		Down:     c.downSetLocked(),
		Replicas: map[string]map[string][]Version{},
	}
	for _, id := range c.order {
		if key != "" {
			resp.Replicas[id] = map[string][]Version{key: c.replica[id].versions(key)}
		} else {
			resp.Replicas[id] = c.replica[id].snapshot()
		}
	}
	return resp
}

// Reset 清空全部副本数据与故障状态（测试与 demo 重复运行用）。
func (c *Cluster) Reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, id := range c.order {
		c.replica[id].reset()
	}
	c.down = map[string]bool{}
	c.counters = map[string]int{}
	c.writes = 0
}

// maximalVersions 在版本集合中保留“最大”版本：
// 若 A 的钟严格先于 B，则 A 被支配、剪枝；互不比较（并发）者全部保留，
// 即兄弟版本/冲突。同 ID/同钟去重。输出按 ID 排序，保证响应稳定。
func maximalVersions(in []Version) []Version {
	uniq := make([]Version, 0, len(in))
	seenID := map[string]bool{}
	seenClock := []Clock{}
	for _, v := range in {
		if seenID[v.ID] {
			continue
		}
		dup := false
		for _, ck := range seenClock {
			if ClockEqual(v.Clock, ck) {
				dup = true
				break
			}
		}
		if dup {
			continue
		}
		seenID[v.ID] = true
		seenClock = append(seenClock, v.Clock)
		uniq = append(uniq, v)
	}
	out := make([]Version, 0, len(uniq))
	for i, vi := range uniq {
		dominated := false
		for j, vj := range uniq {
			if i == j {
				continue
			}
			if ClockLess(vi.Clock, vj.Clock) {
				dominated = true
				break
			}
		}
		if !dominated {
			out = append(out, vi)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func versionSetEqual(a, b []Version) bool {
	if len(a) != len(b) {
		return false
	}
	ids := map[string]bool{}
	for _, v := range a {
		ids[v.ID] = true
	}
	for _, v := range b {
		if !ids[v.ID] {
			return false
		}
	}
	return true
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func sortedKeysBool(m map[string][]Version) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func newVersionID(seq int, coord string) string {
	b := make([]byte, 3)
	_, _ = rand.Read(b)
	return fmt.Sprintf("w%04d-%s-%s", seq, coord, hex.EncodeToString(b))
}
