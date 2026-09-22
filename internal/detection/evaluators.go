package detection

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"activityguard/internal/models"

	"gorm.io/gorm"
)

func sortTime(ts []time.Time) { sort.Slice(ts, func(i, j int) bool { return ts[i].Before(ts[j]) }) }

// evalDownloadBurst 重算受影响的 10 分钟对齐时间桶。
// 桶按 UTC 计算（UTC 桶与员工本地的“10 分钟时长”等价，仅展示时再换算时区）。
func (e *Engine) evalDownloadBurst(tx *gorm.DB, emp *models.Employee, rules *ActiveRules, minOcc, maxOcc, now time.Time, loc *time.Location) error {
	rule, ok := rules.get(models.RuleDownloadBurst)
	if !ok {
		return nil
	}
	p, err := decodeParams[BurstParams](rule)
	if err != nil {
		return err
	}
	p = burstDefaults(p)
	win := time.Duration(p.WindowMinutes) * time.Minute

	firstBucket := minOcc.Truncate(win)
	lastBucket := maxOcc.Truncate(win)

	// 在 Go 侧按 UTC 截断分桶，不依赖 MySQL 会话时区。
	var times []time.Time
	if err := tx.Model(&models.Event{}).
		Select("occurred_at").
		Where("employee_id = ? AND event_type = ? AND occurred_at >= ? AND occurred_at < ?",
			emp.ID, models.EventFileDownload, firstBucket, lastBucket.Add(win)).
		Find(&times).Error; err != nil {
		return err
	}
	counts := make(map[time.Time]int)
	for _, t := range times {
		counts[t.Truncate(win)]++
	}

	// 事件只增不减，空桶不可能曾触发；只需处理非空桶，避免长范围补算时逐桶查库。
	buckets := make([]time.Time, 0, len(counts))
	for b := range counts {
		buckets = append(buckets, b)
	}
	sortTime(buckets)
	for _, b := range buckets {
		c := counts[b]
		start, end := b, b.Add(win)
		dedup := fmt.Sprintf("%s:v%d:%s:%d", models.RuleDownloadBurst, rule.Version, emp.ID, b.Unix())
		if c <= p.Threshold {
			// 补算后窗口不再越限（理论上只增不减，保留以维持通用语义）。
			if err := e.autoResolve(tx, dedup, "below_threshold_after_recompute", now); err != nil {
				return err
			}
			continue
		}
		var evIDs []string
		if err := tx.Model(&models.Event{}).
			Where("employee_id = ? AND event_type = ? AND occurred_at >= ? AND occurred_at < ?",
				emp.ID, models.EventFileDownload, start, end).
			Order("occurred_at ASC").
			Limit(1001).
			Pluck("event_id", &evIDs).Error; err != nil {
			return err
		}
		truncated := false
		if len(evIDs) == 1001 {
			truncated = true
			evIDs = evIDs[:1000]
		}
		ev := burstEvidence{
			RuleKey: models.RuleDownloadBurst, Count: c,
			WindowMinutes: p.WindowMinutes, Threshold: p.Threshold,
			EventIDs: evIDs, EventIDsTruncated: truncated,
			BucketStartLocal: start.In(loc).Format(time.RFC3339),
			BucketEndLocal:   end.In(loc).Format(time.RFC3339),
		}
		raw, _ := json.Marshal(ev)
		in := alertInput{
			RuleKey: models.RuleDownloadBurst, RuleVersion: rule.Version,
			EmployeeID: emp.ID, DepartmentID: emp.DepartmentID,
			Severity: models.SeverityHigh,
			Title:    fmt.Sprintf("%s 10 分钟内下载 %d 个文件（超过 %d）", emp.Name, c, p.Threshold),
			Summary: fmt.Sprintf("%s–%s 本地时段内下载文件 %d 个，阈值为 %d（10 分钟对齐窗口）。",
				start.In(loc).Format("01-02 15:04"), end.In(loc).Format("01-02 15:04"), c, p.Threshold),
			DedupKey:    dedup,
			Evidence:    raw,
			WindowStart: &start, WindowEnd: &end,
			EventTime: end,
		}
		if err := e.upsertAlert(tx, in, now); err != nil {
			return err
		}
	}
	return nil
}

type burstEvidence struct {
	RuleKey           string   `json:"rule_key"`
	Count             int      `json:"count"`
	WindowMinutes     int      `json:"window_minutes"`
	Threshold         int      `json:"threshold"`
	EventIDs          []string `json:"event_ids"`
	EventIDsTruncated bool     `json:"event_ids_truncated"`
	BucketStartLocal  string   `json:"bucket_start_local"`
	BucketEndLocal    string   `json:"bucket_end_local"`
}

// evalFirstUSB 以员工“有记录以来最早 USB 事件”为唯一依据，dedup_key 只与员工绑定。
// 乱序补录更早的 USB 时，upsert 自动刷新首次时间与依据，不会产生第二条告警。
func (e *Engine) evalFirstUSB(tx *gorm.DB, emp *models.Employee, rules *ActiveRules, now time.Time) error {
	rule, ok := rules.get(models.RuleFirstUSB)
	if !ok {
		return nil
	}
	var first models.Event
	err := tx.Where("employee_id = ? AND event_type = ?", emp.ID, models.EventUSB).
		Order("occurred_at ASC").Limit(1).First(&first).Error
	if err == gorm.ErrRecordNotFound {
		return nil
	}
	if err != nil {
		return err
	}

	var meta map[string]any
	_ = json.Unmarshal(first.Metadata, &meta)
	raw, _ := json.Marshal(map[string]any{
		"rule_key":         models.RuleFirstUSB,
		"event_id":         first.EventID,
		"first_usb_at_utc": first.OccurredAt.Format(time.RFC3339),
		"metadata":         meta,
	})
	wStart, wEnd := first.OccurredAt, first.OccurredAt.Add(time.Minute)
	in := alertInput{
		RuleKey: models.RuleFirstUSB, RuleVersion: rule.Version,
		EmployeeID: emp.ID, DepartmentID: emp.DepartmentID,
		Severity: models.SeverityMedium,
		Title:    fmt.Sprintf("%s 首次使用 USB 设备", emp.Name),
		Summary:  fmt.Sprintf("记录到该员工首次 USB 接入，事件 %s。", first.EventID),
		DedupKey: fmt.Sprintf("%s:v%d:%s", models.RuleFirstUSB, rule.Version, emp.ID),
		Evidence: raw, WindowStart: &wStart, WindowEnd: &wEnd,
		EventTime: first.OccurredAt,
	}
	return e.upsertAlert(tx, in, now)
}

// evalNight 枚举受影响的全部本地夜间窗口（含跨日），逐窗口 upsert。
func (e *Engine) evalNight(tx *gorm.DB, emp *models.Employee, rules *ActiveRules, minOcc, maxOcc, now time.Time, loc *time.Location) error {
	rule, ok := rules.get(models.RuleNightActivity)
	if !ok {
		return nil
	}
	p, err := decodeParams[NightParams](rule)
	if err != nil {
		return err
	}
	p = nightDefaults(p)

	// 先用“本批事件”确定受影响的夜间标签（含跨日，按本地时区换算）。
	var batchEvents []models.Event
	if err := tx.Where("employee_id = ? AND occurred_at >= ? AND occurred_at <= ?",
		emp.ID, minOcc, maxOcc).
		Order("occurred_at ASC").
		Find(&batchEvents).Error; err != nil {
		return err
	}
	labelSet := make(map[time.Time]bool)
	var labelOrder []time.Time
	for _, ev := range batchEvents {
		if label, ok := nightLabel(ev.OccurredAt.In(loc), p.StartHour, p.EndHour); ok {
			if !labelSet[label] {
				labelSet[label] = true
				labelOrder = append(labelOrder, label)
			}
		}
	}
	sortTime(labelOrder)

	// 每个受影响夜窗口重新加载“完整窗口内的全部事件”，
	// 保证分批次/延迟到达时 evidence 与计数始终是该夜的全量结果。
	for _, label := range labelOrder {
		start, end := nightWindow(label, loc, p.StartHour, p.EndHour)
		var events []models.Event
		if err := tx.Where("employee_id = ? AND occurred_at >= ? AND occurred_at < ?",
			emp.ID, start, end).
			Order("occurred_at ASC").
			Limit(1001).
			Find(&events).Error; err != nil {
			return err
		}
		dedup := fmt.Sprintf("%s:v%d:%s:%s", models.RuleNightActivity, rule.Version, emp.ID, label.Format("2006-01-02"))
		{
			ids := make([]string, 0, len(events))
			typeCounts := map[string]int{
				models.EventLogin: 0, models.EventFileDownload: 0, models.EventUSB: 0,
			}
			last := start
			for _, ev := range events {
				ids = append(ids, ev.EventID)
				typeCounts[ev.EventType]++
				if ev.OccurredAt.After(last) {
					last = ev.OccurredAt
				}
			}
			truncated := false
			if len(ids) > 1000 {
				ids = ids[:1000]
				truncated = true
			}
			raw, _ := json.Marshal(map[string]any{
				"rule_key":            models.RuleNightActivity,
				"night_label_local":   label.Format("2006-01-02"),
				"window_start_utc":    start.Format(time.RFC3339),
				"window_end_utc":      end.Format(time.RFC3339),
				"window_start_local":  start.In(loc).Format(time.RFC3339),
				"window_end_local":    end.In(loc).Format(time.RFC3339),
				"event_count":         len(events),
				"counts_by_type":      typeCounts,
				"event_ids":           ids,
				"event_ids_truncated": truncated,
			})
			in := alertInput{
				RuleKey: models.RuleNightActivity, RuleVersion: rule.Version,
				EmployeeID: emp.ID, DepartmentID: emp.DepartmentID,
				Severity: models.SeverityMedium,
				Title:    fmt.Sprintf("%s 在夜间时段活动（%s）", emp.Name, label.In(loc).Format("01-02")),
				Summary: fmt.Sprintf("%s 当地夜间 %02d:00–次日 %02d:00 共记录 %d 个事件。",
					emp.Name, p.StartHour, p.EndHour, len(events)),
				DedupKey: dedup, Evidence: raw,
				WindowStart: &start, WindowEnd: &end,
				EventTime: last,
			}
			if err := e.upsertAlert(tx, in, now); err != nil {
				return err
			}
		}
	}
	return nil
}
