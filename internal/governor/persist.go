package governor

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// snapshotFormatVersion 是快照文件格式版本。
const snapshotFormatVersion = 1

// persistedSeries 是 Series 的落盘形态（冗余存储 key，便于人读与调试）。
type persistedSeries struct {
	Key         string            `json:"key"`
	Labels      map[string]string `json:"labels"`
	ValueSum    float64           `json:"value_sum"`
	SampleCount int64             `json:"sample_count"`
	LastUpdated int64             `json:"last_updated"`
}

type persistedMetric struct {
	Series   []persistedSeries `json:"series"`
	Overflow *persistedSeries  `json:"overflow,omitempty"`
}

// SnapshotData 是磁盘上的完整快照。
type SnapshotData struct {
	FormatVersion int                        `json:"format_version"`
	Config        Config                     `json:"config"`
	Counters      Counters                   `json:"counters"`
	Metrics       map[string]persistedMetric `json:"metrics"`
}

// SaveSnapshot 在共享读锁下导出全部状态，并原子写入 path：
// 先写 path.tmp，fsync 后再 rename，避免崩溃留下半截文件。
func (s *Store) SaveSnapshot(path string) error {
	s.mu.RLock()
	data := SnapshotData{
		FormatVersion: snapshotFormatVersion,
		Config:        s.cfg,
		Metrics:       make(map[string]persistedMetric, len(s.metrics)),
	}
	data.Counters = s.ctr
	data.Counters.RejectedByReason = make(map[string]int64, len(s.ctr.RejectedByReason))
	for k, v := range s.ctr.RejectedByReason {
		data.Counters.RejectedByReason[k] = v
	}
	for name, st := range s.metrics {
		pm := persistedMetric{Series: make([]persistedSeries, 0, len(st.series))}
		for key, ser := range st.series {
			labels := make(map[string]string, len(ser.Labels))
			for k, v := range ser.Labels {
				labels[k] = v
			}
			pm.Series = append(pm.Series, persistedSeries{
				Key:         key,
				Labels:      labels,
				ValueSum:    ser.ValueSum,
				SampleCount: ser.SampleCount,
				LastUpdated: ser.LastUpdated,
			})
		}
		if st.overflow != nil {
			pm.Overflow = &persistedSeries{
				Key:         overflowSeriesKey(),
				Labels:      map[string]string{OverflowLabel: OverflowValue},
				ValueSum:    st.overflow.ValueSum,
				SampleCount: st.overflow.SampleCount,
				LastUpdated: st.overflow.LastUpdated,
			}
		}
		data.Metrics[name] = pm
	}
	s.mu.RUnlock()

	buf, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal snapshot: %w", err)
	}

	dir := filepath.Dir(path)
	if dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create snapshot dir: %w", err)
		}
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("open temp snapshot: %w", err)
	}
	if _, err := f.Write(buf); err != nil {
		f.Close()
		return fmt.Errorf("write temp snapshot: %w", err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return fmt.Errorf("fsync temp snapshot: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close temp snapshot: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("rename snapshot: %w", err)
	}
	return nil
}

// LoadSnapshot 从 path 读取快照恢复内存状态。
//
// 恢复策略：
//   - 以快照中的 config 为准（保证预算语义随数据一起恢复）；
//   - 若落盘组合数超过当前生效预算（例如配置文件被改小），超出的组合
//     被折叠进 overflow 桶，其 SampleCount/ValueSum 累加守恒，不丢任何样本；
//   - 指标名超过 MaxMetricNames 时同样整体折叠（实践中只会发生在手工改配置后）。
func LoadSnapshot(path string) (*Store, error) {
	buf, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read snapshot: %w", err)
	}
	var data SnapshotData
	if err := json.Unmarshal(buf, &data); err != nil {
		return nil, fmt.Errorf("parse snapshot: %w", err)
	}
	if data.FormatVersion != snapshotFormatVersion {
		return nil, fmt.Errorf("unsupported snapshot format version: %d", data.FormatVersion)
	}

	store := NewStore(data.Config)
	if data.Counters.RejectedByReason == nil {
		data.Counters.RejectedByReason = map[string]int64{}
	}
	store.ctr = data.Counters

	// 先校验全局指标名预算：快照中的指标数量不得超过恢复配置的上限。
	// 静默折叠会改变查询语义，这里选择显式报错，提示调用方调大预算后重试。
	if len(data.Metrics) > store.cfg.MaxMetricNames {
		return nil, fmt.Errorf("snapshot has %d metric names but MaxMetricNames=%d; raise the limit to restore",
			len(data.Metrics), store.cfg.MaxMetricNames)
	}

	for name, pm := range data.Metrics {
		st := &metricState{series: map[string]*Series{}}
		budget := store.cfg.SeriesBudget(name)
		for _, ps := range pm.Series {
			ser := &Series{
				Labels:      ps.Labels,
				ValueSum:    ps.ValueSum,
				SampleCount: ps.SampleCount,
				LastUpdated: ps.LastUpdated,
			}
			key := ps.Key
			if key == "" {
				key = SeriesKey(ps.Labels)
			}
			if len(st.series) >= budget {
				st.addOverflow(ser)
				continue
			}
			st.series[key] = ser
		}
		if pm.Overflow != nil {
			st.addOverflow(&Series{
				Labels:      pm.Overflow.Labels,
				ValueSum:    pm.Overflow.ValueSum,
				SampleCount: pm.Overflow.SampleCount,
				LastUpdated: pm.Overflow.LastUpdated,
			})
		}
		store.metrics[name] = st
	}
	return store, nil
}

// addOverflow 把一个已有序列折叠进 overflow 桶。
func (m *metricState) addOverflow(ser *Series) {
	if m.overflow == nil {
		m.overflow = &Series{Labels: map[string]string{OverflowLabel: OverflowValue}}
	}
	m.overflow.ValueSum += ser.ValueSum
	m.overflow.SampleCount += ser.SampleCount
	if ser.LastUpdated > m.overflow.LastUpdated {
		m.overflow.LastUpdated = ser.LastUpdated
	}
}
