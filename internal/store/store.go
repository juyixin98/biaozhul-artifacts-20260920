// Package store 是内存存储 + 本地持久化层。
//
// 三层结构：
//   - raw：原始样本（按幂等 ID 存放），用于精确修订与测试期重算基准；
//   - minutes：60s 桶，count/sum/min/max；
//   - hours：3600s 桶，永远由分钟桶折叠而来。
//
// 写入是增量的：新样本直接增量并入分钟桶；小时桶永远由分钟桶折叠
// （每批写入结束时折叠受影响小时）。修订（迟到数据覆盖旧样本）时，
// 受影响的分钟桶用该桶内原始样本重建，随后其父小时桶重新折叠
// ——修订因此沿层向上传播，且小时层绝不会用“均值的均值”更新。
package store

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"

	"metricsink/internal/agg"
	"metricsink/internal/model"
)

const (
	snapshotName = "snapshot.json"
	oplogName    = "oplog.jsonl"
	snapshotVer  = 1
)

// Reject 记录被拒绝的单个样本及原因。
type Reject struct {
	Sample model.Sample `json:"sample"`
	Reason string       `json:"reason"`
}

// Report 是一次批量摄入的处理结果。
type Report struct {
	Inserted  int      `json:"inserted"`
	Revised   int      `json:"revised"`
	Duplicate int      `json:"duplicate"`
	Rejected  []Reject `json:"rejected,omitempty"`
}

// index 是单条时间序列的全部数据。
type index struct {
	metric string
	labels map[string]string
	// raw：样本 ID -> 原始样本
	raw map[string]*model.Sample
	// minutes/hours：桶起点（Unix 秒）-> 聚合桶
	minutes map[int64]*agg.Bucket
	hours   map[int64]*agg.Bucket
}

func newIndex(metric string, labels map[string]string) *index {
	return &index{
		metric:  metric,
		labels:  labels,
		raw:     map[string]*model.Sample{},
		minutes: map[int64]*agg.Bucket{},
		hours:   map[int64]*agg.Bucket{},
	}
}

// Store 是全部序列的线程安全存储。
type Store struct {
	mu sync.Mutex

	dir   string
	oplog *os.File
	seq   uint64

	series    map[model.SeriesKey]*index
	where     map[string]model.SeriesKey // 活跃样本 ID -> 序列键
	forgotten map[string]model.SeriesKey // 已删除/驱逐 ID -> 原序列键（ID 不可复用）
}

// Open 打开（或创建）数据目录并恢复状态。
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("创建数据目录: %w", err)
	}
	s := &Store{
		dir:       dir,
		series:    map[model.SeriesKey]*index{},
		where:     map[string]model.SeriesKey{},
		forgotten: map[string]model.SeriesKey{},
	}
	if err := s.loadSnapshot(); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, oplogName),
		os.O_CREATE|os.O_RDWR|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("打开操作日志: %w", err)
	}
	s.oplog = f
	if err := s.replayOplog(); err != nil {
		return nil, err
	}
	return s, nil
}

// Ingest 批量摄入样本。已存在的 ID（且未被删除）按修订处理；
// 内容完全相同视为重复（幂等）。被删除的 ID 永久拒绝。
func (s *Store) Ingest(samples []model.Sample) (Report, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	var rep Report
	type planned struct {
		smp model.Sample
		old *model.Sample // 非空表示修订
	}
	var accepted []planned

	for _, smp := range samples {
		if err := smp.Validate(); err != nil {
			rep.Rejected = append(rep.Rejected, Reject{smp, err.Error()})
			continue
		}
		if math.IsNaN(smp.Value) || math.IsInf(smp.Value, 0) {
			rep.Rejected = append(rep.Rejected, Reject{smp, "value 必须是有限数值"})
			continue
		}
		id := smp.ID
		if id == "" {
			s.seq++
			id = fmt.Sprintf("auto-%d", s.seq)
			smp.ID = id
		}
		if _, gone := s.forgotten[id]; gone {
			rep.Rejected = append(rep.Rejected, Reject{smp,
				"样本 ID 已被删除或已超出原始保留窗口，不可复用"})
			continue
		}
		var old *model.Sample
		if sk, ok := s.where[id]; ok {
			old = s.series[sk].raw[id]
		}
		if old != nil && old.Key() == smp.Key() &&
			old.Ts == smp.Ts && old.Value == smp.Value {
			rep.Duplicate++
			continue
		}
		accepted = append(accepted, planned{smp: smp, old: old})
	}

	if len(accepted) == 0 {
		return rep, nil
	}

	// 先持久化（整批一次 fsync），成功后才改内存，保证崩溃不丢已确认写入。
	entries := make([]opEntry, 0, len(accepted))
	for _, p := range accepted {
		s.seq++
		entries = append(entries, opEntry{Op: opUpsert, Seq: s.seq, Sample: &p.smp})
	}
	if err := s.appendOps(entries); err != nil {
		return rep, err
	}

	// 受影响的分钟桶（修订路径需要重建）与小时桶（折叠）。
	dirtyMinutes := map[model.SeriesKey]map[int64]struct{}{}
	dirtyHours := map[model.SeriesKey]map[int64]struct{}{}
	mark := func(m map[model.SeriesKey]map[int64]struct{}, sk model.SeriesKey, t int64) {
		set, ok := m[sk]
		if !ok {
			set = map[int64]struct{}{}
			m[sk] = set
		}
		set[t] = struct{}{}
	}

	for _, p := range accepted {
		smp := p.smp
		sk := smp.Key()
		dst := s.series[sk]
		if dst == nil {
			dst = newIndex(smp.Metric, smp.Labels)
			s.series[sk] = dst
		}

		if p.old == nil {
			// 纯新增：增量并入分钟桶。小时桶不在此处直接并入，
			// 批次结束后由分钟桶统一折叠，保证“小时永远来自分钟”。
			mStart := model.FloorToWindow(smp.Ts, model.MinuteWindow)
			mb := dst.minutes[mStart]
			if mb == nil {
				mb = &agg.Bucket{}
				dst.minutes[mStart] = mb
			}
			mb.Add(smp.Value)
			mark(dirtyHours, sk, model.FloorToWindow(smp.Ts, model.HourWindow))

			dst.raw[smp.ID] = &smp
			s.where[smp.ID] = sk
			rep.Inserted++
			continue
		}

		// 修订：摘掉旧样本、放入新样本，分钟桶稍后按原始样本重建。
		rep.Revised++
		oldSK := p.old.Key()
		oldIdx := s.series[oldSK]
		delete(oldIdx.raw, smp.ID)
		mark(dirtyMinutes, oldSK, model.FloorToWindow(p.old.Ts, model.MinuteWindow))
		mark(dirtyHours, oldSK, model.FloorToWindow(p.old.Ts, model.HourWindow))

		dst.raw[smp.ID] = &smp
		s.where[smp.ID] = sk
		mark(dirtyMinutes, sk, model.FloorToWindow(smp.Ts, model.MinuteWindow))
		mark(dirtyHours, sk, model.FloorToWindow(smp.Ts, model.HourWindow))
	}

	s.rebuildMinutes(dirtyMinutes)
	s.foldHours(dirtyHours)
	return rep, nil
}

// Delete 按 ID 删除样本。删除是显式订正：会从分钟/小时层传播移除效果。
// 注意：分钟/小时桶只保存充分统计量，删除原始样本后 min/max 只能用
// 剩余原始样本重算；若原始样本已被保留窗口驱逐，则无法精确删除
// （详见 Evict 与 README“不可恢复的细节”）。
func (s *Store) Delete(ids []string) (deleted int, missing []string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	dirtyMinutes := map[model.SeriesKey]map[int64]struct{}{}
	dirtyHours := map[model.SeriesKey]map[int64]struct{}{}
	mark := func(m map[model.SeriesKey]map[int64]struct{}, sk model.SeriesKey, t int64) {
		set, ok := m[sk]
		if !ok {
			set = map[int64]struct{}{}
			m[sk] = set
		}
		set[t] = struct{}{}
	}

	var live []string
	for _, id := range ids {
		sk, ok := s.where[id]
		if !ok {
			if _, gone := s.forgotten[id]; gone {
				missing = append(missing, id+"(已删除)")
			} else {
				missing = append(missing, id+"(不存在)")
			}
			continue
		}
		idx := s.series[sk]
		smp := idx.raw[id]
		delete(idx.raw, id)
		delete(s.where, id)
		s.forgotten[id] = sk
		live = append(live, id)
		deleted++
		mark(dirtyMinutes, sk, model.FloorToWindow(smp.Ts, model.MinuteWindow))
		mark(dirtyHours, sk, model.FloorToWindow(smp.Ts, model.HourWindow))
	}
	if len(live) == 0 {
		return deleted, missing, nil
	}
	s.seq++
	if err := s.appendOps([]opEntry{{Op: opDelete, Seq: s.seq, IDs: live}}); err != nil {
		return deleted, missing, err
	}
	s.rebuildMinutes(dirtyMinutes)
	s.foldHours(dirtyHours)
	return deleted, missing, nil
}

// EvictResult 是原始保留窗口驱逐的结果。
type EvictResult struct {
	Cutoff  int64 `json:"cutoff"`
	Evicted int   `json:"evicted"`
}

// Evict 驱逐 ts < cutoff 的原始样本以释放空间。
// 分钟/小时聚合桶原样保留（压缩的意义），被驱逐 ID 永久不可修订/复用。
// 这是有损操作：被驱逐样本无法再参与精确重算或订正传播。
func (s *Store) Evict(cutoff int64) (EvictResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	res := EvictResult{Cutoff: cutoff}
	for sk, idx := range s.series {
		for id, smp := range idx.raw {
			if smp.Ts < cutoff {
				delete(idx.raw, id)
				delete(s.where, id)
				s.forgotten[id] = sk
				res.Evicted++
			}
		}
	}
	s.seq++
	if err := s.appendOps([]opEntry{{Op: opEvict, Seq: s.seq, Cutoff: cutoff}}); err != nil {
		return res, err
	}
	return res, nil
}

// rebuildMinutes 用每个受影响分钟桶内的原始样本重建该桶。
// 桶内已无样本时删除键（保留“空桶”语义的缺口，而非残留零值）。
func (s *Store) rebuildMinutes(dirty map[model.SeriesKey]map[int64]struct{}) {
	for sk, starts := range dirty {
		idx := s.series[sk]
		for start := range starts {
			var b agg.Bucket
			for _, smp := range idx.raw {
				if model.FloorToWindow(smp.Ts, model.MinuteWindow) == start {
					b.Add(smp.Value)
				}
			}
			if b.Count == 0 {
				delete(idx.minutes, start)
			} else {
				cp := b
				idx.minutes[start] = &cp
			}
		}
	}
}

// foldHours 用父小时覆盖的 60 个分钟桶重新折叠小时桶。
// 分钟桶缺失即视为空，空小时删除键。
func (s *Store) foldHours(dirty map[model.SeriesKey]map[int64]struct{}) {
	for sk, starts := range dirty {
		idx := s.series[sk]
		for hStart := range starts {
			var b agg.Bucket
			for m := hStart; m < hStart+model.HourWindow; m += model.MinuteWindow {
				if mb, ok := idx.minutes[m]; ok {
					b.Merge(*mb)
				}
			}
			if b.Count == 0 {
				delete(idx.hours, hStart)
			} else {
				cp := b
				idx.hours[hStart] = &cp
			}
		}
	}
}

// rebuildAllHours 在启动重放 oplog 后调用，一次性保证小时层正确。
// 小时桶键从分钟桶派生（无快照时小时层可能为空，不能只遍历已有键）。
func (s *Store) rebuildAllHours() {
	for _, idx := range s.series {
		hourStarts := map[int64]struct{}{}
		for m := range idx.minutes {
			hourStarts[model.FloorToWindow(m, model.HourWindow)] = struct{}{}
		}
		for hStart := range idx.hours {
			hourStarts[hStart] = struct{}{}
		}
		for hStart := range hourStarts {
			var b agg.Bucket
			for m := hStart; m < hStart+model.HourWindow; m += model.MinuteWindow {
				if mb, ok := idx.minutes[m]; ok {
					b.Merge(*mb)
				}
			}
			if b.Count == 0 {
				delete(idx.hours, hStart)
			} else {
				cp := b
				idx.hours[hStart] = &cp
			}
		}
	}
}

// MatchSeries 返回满足 metric 与全部标签等值条件的序列，按键排序。
func (s *Store) MatchSeries(metric string, sel map[string]string) []SeriesView {
	s.mu.Lock()
	defer s.mu.Unlock()

	var out []SeriesView
	for sk, idx := range s.series {
		if idx.metric != metric {
			continue
		}
		ok := true
		for k, v := range sel {
			if idx.labels[k] != v {
				ok = false
				break
			}
		}
		if !ok {
			continue
		}
		out = append(out, SeriesView{Key: string(sk), Metric: idx.metric, Labels: cloneLabels(idx.labels)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

// SeriesView 是序列的元信息视图。
type SeriesView struct {
	Key    string            `json:"key"`
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
}

// RawPoints 返回一条序列在 [start,end] 内的原始样本（按时间、ID 排序）。
func (s *Store) RawPoints(seriesKey string, start, end int64) []model.Sample {
	s.mu.Lock()
	defer s.mu.Unlock()

	idx := s.series[model.SeriesKey(seriesKey)]
	if idx == nil {
		return nil
	}
	var out []model.Sample
	for _, smp := range idx.raw {
		if smp.Ts >= start && smp.Ts <= end {
			out = append(out, *smp)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Ts != out[j].Ts {
			return out[i].Ts < out[j].Ts
		}
		return out[i].ID < out[j].ID
	})
	return out
}

// RollupPoints 返回一条序列在 [start,end] 内指定层（60/3600）的桶。
// fillMissing=true 时用 count=0 的空点补齐缺失桶。
func (s *Store) RollupPoints(seriesKey string, window, start, end int64, fillMissing bool) []model.Point {
	s.mu.Lock()
	defer s.mu.Unlock()

	if window != model.MinuteWindow && window != model.HourWindow {
		return nil
	}
	idx := s.series[model.SeriesKey(seriesKey)]
	if idx == nil {
		return nil
	}
	layer := idx.minutes
	if window == model.HourWindow {
		layer = idx.hours
	}
	lo := model.FloorToWindow(start, window)
	hi := model.FloorToWindow(end, window)

	if !fillMissing {
		var starts []int64
		for t := range layer {
			if t >= lo && t <= hi {
				starts = append(starts, t)
			}
		}
		sort.Slice(starts, func(i, j int) bool { return starts[i] < starts[j] })
		out := make([]model.Point, 0, len(starts))
		for _, t := range starts {
			out = append(out, bucketToPoint(t, *layer[t]))
		}
		return out
	}

	capN := int((hi-lo)/window) + 1
	out := make([]model.Point, 0, capN)
	for t := lo; t <= hi; t += window {
		if b, ok := layer[t]; ok {
			out = append(out, bucketToPoint(t, *b))
		} else {
			out = append(out, model.Point{Ts: t})
		}
	}
	return out
}

// RecomputeFromRaw 直接用原始样本重算指定桶宽的聚合——测试与对账基准。
// 这是“真值”路径：查询路径（RollupPoints）必须与它一致。
func (s *Store) RecomputeFromRaw(seriesKey string, window, start, end int64, fillMissing bool) []model.Point {
	s.mu.Lock()
	defer s.mu.Unlock()

	if window != model.MinuteWindow && window != model.HourWindow {
		return nil
	}
	idx := s.series[model.SeriesKey(seriesKey)]
	if idx == nil {
		return nil
	}
	buckets := map[int64]*agg.Bucket{}
	for _, smp := range idx.raw {
		if smp.Ts < start || smp.Ts > end {
			continue
		}
		bk := model.FloorToWindow(smp.Ts, window)
		b := buckets[bk]
		if b == nil {
			b = &agg.Bucket{}
			buckets[bk] = b
		}
		b.Add(smp.Value)
	}
	lo := model.FloorToWindow(start, window)
	hi := model.FloorToWindow(end, window)

	var starts []int64
	if fillMissing {
		for t := lo; t <= hi; t += window {
			starts = append(starts, t)
		}
	} else {
		for t := range buckets {
			starts = append(starts, t)
		}
		sort.Slice(starts, func(i, j int) bool { return starts[i] < starts[j] })
	}
	out := make([]model.Point, 0, len(starts))
	for _, t := range starts {
		if b, ok := buckets[t]; ok {
			out = append(out, bucketToPoint(t, *b))
		} else {
			out = append(out, model.Point{Ts: t})
		}
	}
	return out
}

func bucketToPoint(ts int64, b agg.Bucket) model.Point {
	p := model.Point{Ts: ts, Count: b.Count, Sum: b.Sum, Min: b.Min, Max: b.Max}
	if b.Count > 0 {
		avg := b.Avg()
		p.Avg = &avg
	}
	return p
}

func cloneLabels(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// Close 落快照并关闭日志。
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.saveSnapshotLocked(); err != nil {
		return err
	}
	return s.oplog.Close()
}

// SaveSnapshot 触发一次快照（快照后截断重放日志）。
func (s *Store) SaveSnapshot() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.saveSnapshotLocked()
}

// ---------- 持久化：JSON 快照 + JSONL 操作日志 ----------

const (
	opUpsert = "upsert"
	opDelete = "delete"
	opEvict  = "evict"
)

type opEntry struct {
	Op     string        `json:"op"`
	Seq    uint64        `json:"seq"`
	Sample *model.Sample `json:"sample,omitempty"`
	IDs    []string      `json:"ids,omitempty"`
	Cutoff int64         `json:"cutoff,omitempty"`
}

func (s *Store) appendOps(entries []opEntry) error {
	enc := json.NewEncoder(s.oplog)
	for i := range entries {
		if err := enc.Encode(&entries[i]); err != nil {
			return fmt.Errorf("写操作日志: %w", err)
		}
	}
	if err := s.oplog.Sync(); err != nil {
		return fmt.Errorf("刷盘操作日志: %w", err)
	}
	return nil
}

type seriesSnapshot struct {
	Key     string                `json:"key"`
	Metric  string                `json:"metric"`
	Labels  map[string]string     `json:"labels"`
	Raw     []model.Sample        `json:"raw"`
	Minutes map[string]agg.Bucket `json:"minutes"`
	Hours   map[string]agg.Bucket `json:"hours"`
}

type snapshotFile struct {
	Version   int               `json:"version"`
	Seq       uint64            `json:"seq"`
	Series    []seriesSnapshot  `json:"series"`
	Forgotten map[string]string `json:"forgotten"`
	Where     map[string]string `json:"where"`
}

func (s *Store) saveSnapshotLocked() error {
	sf := snapshotFile{
		Version:   snapshotVer,
		Seq:       s.seq,
		Forgotten: map[string]string{},
		Where:     map[string]string{},
	}
	for id, sk := range s.forgotten {
		sf.Forgotten[id] = string(sk)
	}
	for id, sk := range s.where {
		sf.Where[id] = string(sk)
	}
	for sk, idx := range s.series {
		snap := seriesSnapshot{
			Key:     string(sk),
			Metric:  idx.metric,
			Labels:  cloneLabels(idx.labels),
			Minutes: map[string]agg.Bucket{},
			Hours:   map[string]agg.Bucket{},
		}
		for _, smp := range idx.raw {
			snap.Raw = append(snap.Raw, *smp)
		}
		sort.Slice(snap.Raw, func(i, j int) bool {
			if snap.Raw[i].Ts != snap.Raw[j].Ts {
				return snap.Raw[i].Ts < snap.Raw[j].Ts
			}
			return snap.Raw[i].ID < snap.Raw[j].ID
		})
		for t, b := range idx.minutes {
			snap.Minutes[strconv.FormatInt(t, 10)] = *b
		}
		for t, b := range idx.hours {
			snap.Hours[strconv.FormatInt(t, 10)] = *b
		}
		sf.Series = append(sf.Series, snap)
	}
	sort.Slice(sf.Series, func(i, j int) bool { return sf.Series[i].Key < sf.Series[j].Key })

	tmp := filepath.Join(s.dir, snapshotName+".tmp")
	f, err := os.Create(tmp)
	if err != nil {
		return fmt.Errorf("写快照: %w", err)
	}
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(&sf); err != nil {
		f.Close()
		return fmt.Errorf("编码快照: %w", err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return fmt.Errorf("刷盘快照: %w", err)
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, filepath.Join(s.dir, snapshotName)); err != nil {
		return fmt.Errorf("提交快照: %w", err)
	}
	// 快照已包含全部状态，重放日志可安全截断。
	if err := s.oplog.Truncate(0); err != nil {
		return fmt.Errorf("截断操作日志: %w", err)
	}
	if _, err := s.oplog.Seek(0, io.SeekStart); err != nil {
		return err
	}
	return s.oplog.Sync()
}

func (s *Store) loadSnapshot() error {
	data, err := os.ReadFile(filepath.Join(s.dir, snapshotName))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("读快照: %w", err)
	}
	var sf snapshotFile
	if err := json.Unmarshal(data, &sf); err != nil {
		return fmt.Errorf("解析快照: %w", err)
	}
	if sf.Version != snapshotVer {
		return fmt.Errorf("不支持的快照版本: %d", sf.Version)
	}
	s.seq = sf.Seq
	for id, sk := range sf.Forgotten {
		s.forgotten[id] = model.SeriesKey(sk)
	}
	for _, snap := range sf.Series {
		idx := newIndex(snap.Metric, snap.Labels)
		for i := range snap.Raw {
			smp := snap.Raw[i]
			idx.raw[smp.ID] = &smp
			s.where[smp.ID] = model.SeriesKey(snap.Key)
		}
		for tstr, b := range snap.Minutes {
			t, err := strconv.ParseInt(tstr, 10, 64)
			if err != nil {
				return fmt.Errorf("快照分钟桶键 %q: %w", tstr, err)
			}
			bc := b
			idx.minutes[t] = &bc
		}
		for tstr, b := range snap.Hours {
			t, err := strconv.ParseInt(tstr, 10, 64)
			if err != nil {
				return fmt.Errorf("快照小时桶键 %q: %w", tstr, err)
			}
			bc := b
			idx.hours[t] = &bc
		}
		s.series[model.SeriesKey(snap.Key)] = idx
	}
	return nil
}

// replayOplog 重放快照之后的操作日志。
// upsert/delete 只维护 raw 与分钟桶并收集受影响分钟，
// 全部重放完毕后统一重建受影响分钟、再重建所有小时桶。
func (s *Store) replayOplog() error {
	f, err := os.Open(filepath.Join(s.dir, oplogName))
	if err != nil {
		return fmt.Errorf("读操作日志: %w", err)
	}
	defer f.Close()

	dirtyMinutes := map[model.SeriesKey]map[int64]struct{}{}
	maxSeq := s.seq
	dec := json.NewDecoder(f)
	for {
		var e opEntry
		if err := dec.Decode(&e); err != nil {
			if err == io.EOF {
				break
			}
			return fmt.Errorf("解析操作日志: %w", err)
		}
		if e.Seq > maxSeq {
			maxSeq = e.Seq
		}
		switch e.Op {
		case opUpsert:
			smp := *e.Sample
			sk := smp.Key()
			dst := s.series[sk]
			if dst == nil {
				dst = newIndex(smp.Metric, smp.Labels)
				s.series[sk] = dst
			}
			// 修订：先移除旧位置。
			if oldSK, ok := s.where[smp.ID]; ok {
				old := s.series[oldSK].raw[smp.ID]
				delete(s.series[oldSK].raw, smp.ID)
				addDirty(dirtyMinutes, oldSK, model.FloorToWindow(old.Ts, model.MinuteWindow))
			}
			delete(s.forgotten, smp.ID) // 正常不会出现：forgotten ID 不允许再写
			dst.raw[smp.ID] = &smp
			s.where[smp.ID] = sk
			addDirty(dirtyMinutes, sk, model.FloorToWindow(smp.Ts, model.MinuteWindow))
		case opDelete:
			for _, id := range e.IDs {
				sk, ok := s.where[id]
				if !ok {
					continue
				}
				idx := s.series[sk]
				smp := idx.raw[id]
				delete(idx.raw, id)
				delete(s.where, id)
				s.forgotten[id] = sk
				addDirty(dirtyMinutes, sk, model.FloorToWindow(smp.Ts, model.MinuteWindow))
			}
		case opEvict:
			for sk, idx := range s.series {
				for id, smp := range idx.raw {
					if smp.Ts < e.Cutoff {
						delete(idx.raw, id)
						delete(s.where, id)
						s.forgotten[id] = sk
					}
				}
			}
		default:
			return fmt.Errorf("未知操作日志类型 %q", e.Op)
		}
	}
	s.seq = maxSeq
	s.rebuildMinutes(dirtyMinutes)
	s.rebuildAllHours()
	return nil
}

func addDirty(m map[model.SeriesKey]map[int64]struct{}, sk model.SeriesKey, t int64) {
	set, ok := m[sk]
	if !ok {
		set = map[int64]struct{}{}
		m[sk] = set
	}
	set[t] = struct{}{}
}
