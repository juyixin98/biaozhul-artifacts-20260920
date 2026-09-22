// Package syncer 实现节点区间同步引擎。
//
// 核心安全性质：
//
//  1. 只有"连续校验通过的前缀"能提交到 SQLite，从而推进检查点；任何缺口之后的
//     数据即使已经拉回、验签通过，也只能停留在有界乱序缓存中，不能推进检查点。
//  2. 远端节点 ChainTip "宣称"的高度仅用于估计目标，绝不等同于已验证进度；
//     可信样例（TrustedSample）给出目标链尖与检查点哈希，作为带外信任锚。
//  3. 并行拉取最多 MaxParallel 段（默认 4）；乱序结果进入有界缓存（按段数计量）。
//  4. 坏段（超时 / gRPC 错误 / 签名失败 / 父哈希错误 / 短段）按轮询偏移换源重试，
//     绝不跳过缺口；全部源都失败则同步失败，检查点停在最后一个连续高度。
//  5. 取消（context 取消）后，在途旧请求返回的结果一律丢弃，绝不写库推进检查点。
//  6. 重启后从 SQLite 中已提交的链尖继续，并用可信样例复核已有检查点。
package syncer

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"nodesync/internal/chain"
	"nodesync/internal/harness"
	"nodesync/internal/storage"
)

// 默认参数。
const (
	DefaultSegmentSize = 8
	DefaultMaxParallel = 4 // 并行拉取最多 4 段
	DefaultWindowSegs  = 8 // 乱序缓存最多容纳 8 段（有界）
	DefaultMaxRetries  = 4 // 单段跨源尝试次数上限
	rpcTimeout         = 1500 * time.Millisecond
)

// Config 是同步引擎配置。
type Config struct {
	Peers         []*harness.Peer
	Store         *storage.Store
	Sample        chain.TrustedSample
	SegmentSize   uint64
	MaxParallel   int
	WindowSegs    int
	MaxSegRetries int
	// TargetTip 为 0 时用样例链尖（优先）或 max(节点宣称高度) 估计。
	// 注意：宣称高度只决定"尝试到哪里"，不决定"验证到哪里"。
	TargetTip uint64
	// RPCTimeout 单次拉取超时；0 用默认值。
	RPCTimeout time.Duration
}

// Summary 是一次同步运行的结果摘要。
type Summary struct {
	VerifiedTip       uint64           `json:"verified_tip"`
	TargetTip         uint64           `json:"target_tip"`
	ReachedTarget     bool             `json:"reached_target"`
	AdvertisedMax     uint64           `json:"advertised_max_height"`
	TrustedSampleTip  uint64           `json:"trusted_sample_tip"`
	SegmentsFetched   int64            `json:"segments_fetched"`
	SegmentsCommitted int64            `json:"segments_committed"`
	SegmentsRejected  int64            `json:"segments_rejected"`
	SegmentsServedBy  map[string]int64 `json:"segments_served_by_node"`
	FetchErrorsByNode map[string]int64 `json:"fetch_errors_by_node"`
	Checkpoints       []string         `json:"checkpoints"`
	Warnings          []string         `json:"warnings"`
	Cancelled         bool             `json:"cancelled"`
}

// 引擎内部错误标记。
var (
	errRecommit   = errors.New("缓存段最终链接校验失败，需要换源重取")
	ErrCheckpoint = errors.New("可信检查点校验失败")
	ErrExhausted  = errors.New("所有源对该区间均失败，不能跳过缺口")
)

type segResult struct {
	start   uint64
	blocks  []*chain.Block
	nodeID  string
	err     error
	peerID  string
	wantLen uint64 // 合法返回的最小长度（尾段允许更短，其余位置短段即故障）
}

type cachedSeg struct {
	blocks []*chain.Block
	nodeID string
}

type fetcher struct {
	start  uint64
	limit  uint64
	minLen uint64 // 合法返回最少区块数（非尾段必须给满，尾段允许到链尖为止）
	peer   *harness.Peer
	result chan segResult
}

type engine struct {
	cfg        Config
	s          *Summary
	next       uint64 // 下一个待提交高度（= 已验证检查点 + 1）
	target     uint64
	rpcTimeout time.Duration
	cache      map[uint64]*cachedSeg // 段起始高度 -> 已内部校验的乱序段
	attempt    map[uint64]int
	inflight   map[uint64]*fetcher
	results    chan segResult
	sem        chan struct{}
	wg         sync.WaitGroup
}

// Run 执行一次同步。可用同一 Store 多次调用以实现取消后 / 重启后续传。
func Run(ctx context.Context, cfg Config) (*Summary, error) {
	cfg = withDefaults(cfg)
	if len(cfg.Peers) == 0 {
		return nil, errors.New("没有配置任何远端节点")
	}

	s := &Summary{
		SegmentsServedBy:  map[string]int64{},
		FetchErrorsByNode: map[string]int64{},
		TrustedSampleTip:  cfg.Sample.TipHeight,
	}

	// ---- 1) 重启恢复：读取已提交链尖，复核可信样例检查点 ----
	next, err := recoverFromStore(ctx, cfg, s)
	if err != nil {
		return s, err
	}

	// ---- 2) 询问各节点"宣称"高度（仅作目标估计与证据展示）----
	advertisedMax := queryAdvertisedTips(ctx, cfg, s)
	s.AdvertisedMax = advertisedMax

	// 对"宣称超过可信样例链尖"的节点做一次真实探测：虚高高度必须取不到，
	// 以此证明宣称高度不代表可验证进度，并留下证据。
	for _, p := range cfg.Peers {
		h, _, perr := p.AdvertisedTip(ctx)
		if perr != nil || h <= cfg.Sample.TipHeight {
			continue
		}
		pctx, pcancel := context.WithTimeout(ctx, 3*time.Second)
		_, _, ferr := p.Fetch(pctx, h, 1)
		pcancel()
		_ = cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event: "advertised_height_probe", HeightFrom: int64(h), HeightTo: int64(h),
			NodeID: p.ID,
			Detail: detailf("节点宣称高度 %d 超出可信链尖 %d；实际拉取结果: %v（证实虚高不可用）",
				h, cfg.Sample.TipHeight, ferr),
		})
	}

	target := cfg.TargetTip
	if cfg.Sample.TipHeight > 0 {
		// 可信样例是带外信任锚：同步目标默认以样例链尖为准；
		// 显式 TargetTip 只允许"只同步到更矮的位置"（用于分阶段/重启演示），
		// 节点"宣称"的更高高度永远不扩大目标。
		if target == 0 || target > cfg.Sample.TipHeight {
			target = cfg.Sample.TipHeight
		}
	} else if target == 0 {
		target = advertisedMax
	}
	s.TargetTip = target
	if advertisedMax > cfg.Sample.TipHeight {
		s.Warnings = append(s.Warnings, fmt.Sprintf(
			"节点宣称最大高度 %d 高于可信样例链尖 %d：虚高部分不会成为已验证进度",
			advertisedMax, cfg.Sample.TipHeight))
	}

	rpcTO := cfg.RPCTimeout
	if rpcTO == 0 {
		rpcTO = rpcTimeout
	}
	e := &engine{
		cfg:        cfg,
		s:          s,
		next:       next,
		target:     target,
		rpcTimeout: rpcTO,
		cache:      map[uint64]*cachedSeg{},
		attempt:    map[uint64]int{},
		inflight:   map[uint64]*fetcher{},
		results:    make(chan segResult, cfg.MaxParallel),
		sem:        make(chan struct{}, cfg.MaxParallel),
	}
	err = e.loop(ctx)

	// ---- 收尾：无论成功 / 失败 / 取消，如实反映已验证检查点 ----
	tip, has, terr := cfg.Store.TipHeight()
	if terr == nil && has {
		s.VerifiedTip = tip
	}
	s.ReachedTarget = err == nil && s.VerifiedTip >= target
	if errors.Is(context.Cause(ctx), context.Canceled) || ctx.Err() == context.Canceled {
		s.Cancelled = true
	}
	return s, err
}

func withDefaults(cfg Config) Config {
	if cfg.SegmentSize == 0 {
		cfg.SegmentSize = DefaultSegmentSize
	}
	if cfg.MaxParallel == 0 {
		cfg.MaxParallel = DefaultMaxParallel
	}
	if cfg.WindowSegs == 0 {
		cfg.WindowSegs = DefaultWindowSegs
	}
	if cfg.MaxSegRetries == 0 {
		cfg.MaxSegRetries = DefaultMaxRetries
	}
	return cfg
}

// recoverFromStore 从数据库恢复检查点并复核样例。
func recoverFromStore(ctx context.Context, cfg Config, s *Summary) (uint64, error) {
	tip, hasBlocks, err := cfg.Store.TipHeight()
	if err != nil {
		return 0, err
	}
	if !hasBlocks {
		return 0, nil
	}
	blocks, err := cfg.Store.BlocksMap()
	if err != nil {
		return 0, err
	}
	if err := chain.VerifyCheckpointsUpTo(blocks, cfg.Sample, tip); err != nil {
		return 0, fmt.Errorf("%w: %v", ErrCheckpoint, err)
	}
	_ = cfg.Store.AddEvidence(storage.EvidenceEvent{
		Event:      "resume",
		HeightFrom: int64(tip),
		HeightTo:   int64(tip),
		NodeID:     "-",
		Detail:     detailf("从持久化检查点恢复，已验证高度 0..%d", tip),
	})
	return tip + 1, nil
}

// queryAdvertisedTips 记录各节点宣称状态，返回最大宣称高度。
func queryAdvertisedTips(ctx context.Context, cfg Config, s *Summary) uint64 {
	var maxH uint64
	for _, p := range cfg.Peers {
		tctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		h, hh, perr := p.AdvertisedTip(tctx)
		cancel()
		st := storage.PeerStatus{NodeID: p.ID, AdvertisedHeight: h, AdvertisedHash: fmt.Sprintf("%x", hh)}
		if perr != nil {
			st.LastError = perr.Error()
		} else if h > maxH {
			maxH = h
		}
		_ = cfg.Store.UpsertPeerStatus(st)
		_ = cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event:      "peer_tip",
			HeightFrom: -1,
			HeightTo:   int64(h),
			NodeID:     p.ID,
			Detail:     detailf("宣称高度=%d hash=%x err=%v（未经证实）", h, hh, perr),
		})
	}
	return maxH
}

// segStart 返回包含高度 h 的固定段起点（段按 [k*size, (k+1)*size) 划分）。
func segStart(h, size uint64) uint64 { return (h / size) * size }

// loop 是引擎主循环：所有 SQLite 写入都只在本 goroutine 发生。
func (e *engine) loop(ctx context.Context) error {
	size := e.cfg.SegmentSize

	// 主循环结束时必须等待所有 worker 退出，避免 context 取消后 goroutine 泄漏。
	defer e.wg.Wait()

	for {
		// 完成判定：连续前缀到达目标。
		if e.next > e.target {
			return e.finalCheck(ctx)
		}
		if ctx.Err() != nil {
			return nil // 取消：保留已提交前缀，丢弃一切在途/缓存结果
		}

		e.spawnWindow(ctx)

		// 没有任何在途请求时：要么全部完成，要么存在被重试上限卡死的缺口。
		if len(e.inflight) == 0 {
			st := segStart(e.next, size)
			if _, ok := e.cache[st]; ok {
				if err := e.drainCache(ctx); err != nil {
					if errors.Is(err, errRecommit) {
						continue // 段被拒绝后已清除，重新调度
					}
					return err
				}
				continue
			}
			// 无在途、无缓存、next 仍未到 target：缺口段重试次数耗尽
			if e.next <= e.target {
				return fmt.Errorf("%w: 高度 %d 起的区间，%d 次尝试全部失败",
					ErrExhausted, e.next, e.attempt[st])
			}
			return nil
		}

		select {
		case <-ctx.Done():
			// 取消：不再读取任何在途结果；worker 因 ctx 取消而结束，
			// 其结果写入 buffered channel 后被 GC，绝不推进检查点。
			return nil
		case r := <-e.results:
			if e.handleResult(ctx, r) {
				if err := e.drainCache(ctx); err != nil {
					if errors.Is(err, errRecommit) {
						continue
					}
					return err
				}
			}
		}
	}
}

// spawnWindow 为有界窗口内、尚未缓存 / 在途且未耗尽重试的段启动拉取。
func (e *engine) spawnWindow(ctx context.Context) {
	size := e.cfg.SegmentSize
	base := segStart(e.next, size)
	high := base + uint64(e.cfg.WindowSegs)*size

	for st := base; st <= e.target && st < high; st += size {
		if _, ok := e.cache[st]; ok {
			continue
		}
		if _, ok := e.inflight[st]; ok {
			continue
		}
		if e.attempt[st] >= e.cfg.MaxSegRetries {
			continue
		}
		// 固定请求段长：即使目标不足一整段也请求完整段。
		// 节点只持有到链尖时自然返回更短的尾段；在其它位置返回短段即故障。
		limit := size
		minLen := size
		if st+size > e.target+1 {
			minLen = e.target + 1 - st // 尾段：拿到链尖即合法
		}
		if minLen == 0 {
			continue
		}

		select {
		case e.sem <- struct{}{}: // 获取并行槽（最多 4）
		default:
			return // 槽位已满，本轮先不调度更多段
		}

		// 换源重试：第 k 次尝试从轮换后的源顺序中取第一个；
		// 节点对不持有的区间会返回 NotFound，失败计入换源。
		peer := e.pickPeer(st)
		if peer == nil {
			<-e.sem
			e.attempt[st] = e.cfg.MaxSegRetries
			continue
		}

		f := &fetcher{start: st, limit: limit, minLen: minLen, peer: peer, result: e.results}
		e.inflight[st] = f
		e.attempt[st]++
		e.wg.Add(1)
		go e.fetch(ctx, f)
	}
}

// pickPeer 按"段索引 + 已尝试次数"轮换源：不同段的第一次尝试落在不同源上，
// 使各源的故障都可能被真实触发；某段失败后依次切换下一个源（换源重试）。
func (e *engine) pickPeer(st uint64) *harness.Peer {
	n := len(e.cfg.Peers)
	segIdx := int(st / uint64(e.cfg.SegmentSize))
	idx := (segIdx + e.attempt[st]) % n
	return e.cfg.Peers[idx]
}

// fetch 在 worker goroutine 中执行一次真实 gRPC 拉取与本地校验。
// 注意：不触碰数据库，不推进检查点；结果只投递到 results。
func (e *engine) fetch(parent context.Context, f *fetcher) {
	defer e.wg.Done()
	defer func() { <-e.sem }()

	ctx, cancel := context.WithTimeout(parent, e.rpcTimeout)
	defer cancel()

	blocks, nodeID, err := f.peer.Fetch(ctx, f.start, f.limit)
	r := segResult{start: f.start, blocks: blocks, nodeID: nodeID, err: err, peerID: f.peer.ID, wantLen: f.minLen}
	if err == nil {
		// 内部校验：结构、高度连续、逐块签名、段内哈希链、短段故障判定。
		if verr := verifySegmentInternal(f.start, blocks, f.minLen); verr != nil {
			r.err = verr
		}
	}
	// buffered channel：即使主循环已因取消退出、无人接收，发送也不阻塞，
	// 旧请求的结果随 goroutine 退出被丢弃，永远不会到达写库路径。
	select {
	case f.result <- r:
	case <-parent.Done():
	}
}

// handleResult 处理一个拉取结果；返回 true 表示可能有新的连续前缀可提交。
func (e *engine) handleResult(ctx context.Context, r segResult) bool {
	delete(e.inflight, r.start)
	size := e.cfg.SegmentSize

	if r.err != nil {
		e.s.FetchErrorsByNode[r.peerID]++
		e.s.SegmentsRejected++
		_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event:      "segment_rejected",
			HeightFrom: int64(r.start),
			HeightTo:   int64(r.start + size - 1),
			NodeID:     r.peerID,
			Detail:     detailf("第 %d 次尝试失败: %v", e.attempt[r.start], r.err),
		})
		return false
	}

	e.s.SegmentsFetched++
	e.s.SegmentsServedBy[r.nodeID]++

	// 乱序 / 过期结果处理：
	//  - 段起点已落在 next 之前（取消恢复、并发重复）→ 丢弃
	//  - 超出有界窗口 → 丢弃（窗口随 next 前移后自然会重新调度）
	if r.start < segStart(e.next, size) {
		_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event: "segment_stale", HeightFrom: int64(r.start),
			HeightTo: int64(r.start + uint64(len(r.blocks)) - 1),
			NodeID:   r.nodeID, Detail: "结果到达时检查点已越过该段，丢弃",
		})
		return false
	}
	if _, busy := e.cache[r.start]; busy {
		return false
	}
	e.cache[r.start] = &cachedSeg{blocks: r.blocks, nodeID: r.nodeID}
	_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
		Event:      "segment_received",
		HeightFrom: int64(r.start),
		HeightTo:   int64(r.start + uint64(len(r.blocks)) - 1),
		NodeID:     r.nodeID,
		Detail: detailf("内部校验通过并进入乱序缓存（当前缓存 %d 段，上限 %d）",
			len(e.cache), e.cfg.WindowSegs),
	})
	return true
}

// drainCache 从 next 起逐段提交连续前缀；缺口处停止，绝不跳过。
func (e *engine) drainCache(ctx context.Context) error {
	size := e.cfg.SegmentSize
	for {
		st := segStart(e.next, size)
		cs, ok := e.cache[st]
		if !ok {
			return nil // 遇到缺口：停止推进，等待该段
		}

		// 最终链接校验：相对已提交链（缓存段在拉取时只做了段内校验）。
		if err := verifySegmentAgainstStore(e.cfg.Store, e.next, cs.blocks); err != nil {
			delete(e.cache, st)
			e.s.SegmentsRejected++
			_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
				Event: "segment_rejected", HeightFrom: int64(st),
				HeightTo: int64(st + size - 1), NodeID: cs.nodeID,
				Detail: detailf("缺口补齐时最终链接校验失败，丢弃并换源重取: %v", err),
			})
			return errRecommit
		}

		// 只提交 [next, ...) 部分（首段可能从高度 0 起，而 next>0 是重启恢复情形）。
		blocks := cs.blocks
		commitFrom := uint64(0)
		if blocks[0].Height < e.next {
			commitFrom = e.next - blocks[0].Height
		}
		blocks = blocks[commitFrom:]

		if err := e.cfg.Store.AppendVerifiedSegment(blocks, e.next, cs.nodeID); err != nil {
			return err
		}
		e.s.SegmentsCommitted++
		end := blocks[len(blocks)-1].Height
		_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event:      "checkpoint_advance",
			HeightFrom: int64(e.next), HeightTo: int64(end), NodeID: cs.nodeID,
			Detail: detailf("连续校验通过，检查点 %d -> %d", e.next-1, end),
		})
		e.next = end + 1
		e.s.VerifiedTip = end
		delete(e.cache, st)

		// 每次推进后立即用可信样例复核（只复核已到达高度的检查点）。
		bm, _ := e.cfg.Store.BlocksMap()
		if err := chain.VerifyCheckpointsUpTo(bm, e.cfg.Sample, end); err != nil {
			return fmt.Errorf("%w: %v", ErrCheckpoint, err)
		}
		for h := range e.cfg.Sample.Checkpoints {
			if h == end {
				e.s.Checkpoints = append(e.s.Checkpoints, fmt.Sprintf(
					"检查点高度 %d 已连续验证并提交，哈希与可信样例一致", h))
			}
		}
	}
}

// finalCheck 到达目标后复核已覆盖区间内的样例检查点；
// 若目标已包含样例链尖，则还必须验证链尖哈希一致。
func (e *engine) finalCheck(ctx context.Context) error {
	bm, err := e.cfg.Store.BlocksMap()
	if err != nil {
		return err
	}
	if err := chain.VerifyCheckpointsUpTo(bm, e.cfg.Sample, e.target); err != nil {
		return fmt.Errorf("%w: %v", ErrCheckpoint, err)
	}
	if e.target < e.cfg.Sample.TipHeight {
		_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
			Event: "sync_partial_complete", HeightFrom: 0, HeightTo: int64(e.target),
			NodeID: "-", Detail: detailf("按目标高度 %d 分阶段完成（样例链尖 %d 未要求本轮覆盖）",
				e.target, e.cfg.Sample.TipHeight),
		})
		return nil
	}
	tipB, ok := bm[e.cfg.Sample.TipHeight]
	if !ok {
		return fmt.Errorf("目标高度 %d 未取得", e.cfg.Sample.TipHeight)
	}
	want := e.cfg.Sample.Checkpoints[e.cfg.Sample.TipHeight]
	if want != "" && fmt.Sprintf("%x", tipB.Hash()) != want {
		return fmt.Errorf("%w: 链尖哈希与样例不一致", ErrCheckpoint)
	}
	_ = e.cfg.Store.AddEvidence(storage.EvidenceEvent{
		Event: "sync_complete", HeightFrom: 0, HeightTo: int64(e.cfg.Sample.TipHeight),
		NodeID: "-", Detail: "全链连续验证完成，链尖哈希与可信样例一致",
	})
	return nil
}

// ---- 段校验 ----

// verifySegmentInternal 校验段自身：长度、起点、高度连续、逐块签名、段内哈希链。
// 不访问数据库，因此可在 worker goroutine 中并行执行。
// minLen: 非尾段必须给满；尾段（请求区间超过目标链尖）允许更短，短于 minLen 即故障。
func verifySegmentInternal(start uint64, blocks []*chain.Block, minLen uint64) error {
	if len(blocks) == 0 {
		return fmt.Errorf("高度 %d: 节点返回空段", start)
	}
	if blocks[0].Height != start {
		return fmt.Errorf("高度 %d: 段首高度为 %d", start, blocks[0].Height)
	}
	if uint64(len(blocks)) < minLen {
		return fmt.Errorf("高度 %d: 短段故障，至少需要 %d 块仅返回 %d 块", start, minLen, len(blocks))
	}
	for i, b := range blocks {
		if b.Height != start+uint64(i) {
			return fmt.Errorf("段内高度不连续: 期望 %d 得到 %d", start+uint64(i), b.Height)
		}
		if err := b.VerifyCrypto(); err != nil {
			return err
		}
		if i > 0 {
			prev := blocks[i-1]
			if !bytesEq(prev.Hash(), b.ParentHash) {
				return fmt.Errorf("高度 %d: 段内父哈希错误（错误父哈希故障）", b.Height)
			}
		}
	}
	return nil
}

// verifySegmentAgainstStore 在缺口补齐、准备提交前做最终链接校验。
// 每一个待提交区块的父哈希都必须要么等于段内前驱、要么等于数据库中已提交前驱；
// 段首若接在已提交链之后，必须锚定数据库中的真实哈希（防止恢复时绕过检查点）。
func verifySegmentAgainstStore(store *storage.Store, next uint64, blocks []*chain.Block) error {
	for _, b := range blocks {
		if b.Height < next {
			continue
		}
		if err := b.VerifyCrypto(); err != nil {
			return err
		}
		if b.Height == 0 {
			if !bytesEq(b.ParentHash, chain.GenesisParent) {
				return fmt.Errorf("创世块父哈希哨兵错误")
			}
			continue
		}
		// 优先锚定数据库中已提交的前驱；数据库没有时才用段内前驱。
		stored, ok, err := store.GetBlock(b.Height - 1)
		if err != nil {
			return err
		}
		var prevHash []byte
		if ok {
			prevHash = stored.Hash()
		} else {
			var prev *chain.Block
			for _, pb := range blocks {
				if pb.Height == b.Height-1 {
					prev = pb
					break
				}
			}
			if prev == nil {
				return fmt.Errorf("高度 %d 的前驱未提交（存在缺口），不能跳过", b.Height-1)
			}
			prevHash = prev.Hash()
		}
		if !bytesEq(prevHash, b.ParentHash) {
			return fmt.Errorf("高度 %d: 与已提交链的父哈希不匹配", b.Height)
		}
	}
	return nil
}

func bytesEq(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func detailf(format string, args ...any) string { return fmt.Sprintf(format, args...) }
