package detection

import (
	"encoding/json"
	"fmt"
	"time"

	"activityguard/internal/models"

	"gorm.io/gorm"
)

// evalZScore 重算受影响的本地自然日：每个被评估日的“当日下载量”
// 与该日之前 lookbackDays 个历史自然日的样本比较。
// 历史固定（补算的是当日事件），但一个迟到事件既可能更新“当日”，
// 也可能成为其后若干天的“历史样本”，因此受影响区间为
// [首个事件本地日, 今日]。
//
// 本地日期分桶在 Go 内完成，正确处理夏令时切换；MySQL 会话时区无需时区表。
func (e *Engine) evalZScore(tx *gorm.DB, emp *models.Employee, rules *ActiveRules, minOcc, maxOcc, now time.Time, loc *time.Location) error {
	rule, ok := rules.get(models.RuleZScore)
	if !ok {
		return nil
	}
	p, err := decodeParams[ZScoreParams](rule)
	if err != nil {
		return err
	}
	p = zDefaults(p)

	firstDay := localDate(minOcc, loc)
	today := localDate(now, loc)
	lastDay := localDate(maxOcc, loc)
	if lastDay.After(today) {
		lastDay = today
	}

	// 一次加载评估所需的全部下载事件：最早样本日 = firstDay-lookbackDays。
	rangeStart, _ := dayRangeUTC(firstDay.AddDate(0, 0, -p.LookbackDays), loc)
	_, rangeEnd := dayRangeUTC(lastDay, loc)
	var events []models.Event
	if err := tx.Select("occurred_at").
		Where("employee_id = ? AND event_type = ? AND occurred_at >= ? AND occurred_at < ?",
			emp.ID, models.EventFileDownload, rangeStart, rangeEnd).
		Find(&events).Error; err != nil {
		return err
	}
	counts := map[string]float64{}
	for _, ev := range events {
		d := localDate(ev.OccurredAt, loc).Format("2006-01-02")
		counts[d]++
	}

	for day := firstDay; !day.After(lastDay); day = day.AddDate(0, 0, 1) {
		dayStart, dayEnd := dayRangeUTC(day, loc)
		current := counts[day.Format("2006-01-02")]

		history := make([]float64, p.LookbackDays)
		for i := 0; i < p.LookbackDays; i++ {
			d := day.AddDate(0, 0, -(i + 1)).Format("2006-01-02")
			history[p.LookbackDays-1-i] = counts[d] // 无事件日记 0
		}

		dec := decideZScore(history, current, p)
		dedup := fmt.Sprintf("%s:v%d:%s:%s", models.RuleZScore, rule.Version, emp.ID, day.Format("2006-01-02"))

		if !dec.Triggered {
			// 补算使当日值回落到阈值下（或样本变为不足）：自动消解尚未被人工处理的告警。
			if err := e.autoResolve(tx, dedup, dec.Reason, now); err != nil {
				return err
			}
			continue
		}

		raw, _ := json.Marshal(map[string]any{
			"rule_key":      models.RuleZScore,
			"metric":        p.Metric,
			"local_day":     day.Format("2006-01-02"),
			"current":       dec.Current,
			"mean":          dec.Mean,
			"std":           dec.Std,
			"z":             dec.Z,
			"z_threshold":   p.ZThreshold,
			"sample_days":   dec.SampleDays,
			"positive_days": dec.PositiveDays,
			"lookback_days": p.LookbackDays,
		})
		in := alertInput{
			RuleKey: models.RuleZScore, RuleVersion: rule.Version,
			EmployeeID: emp.ID, DepartmentID: emp.DepartmentID,
			Severity: models.SeverityHigh,
			Title:    fmt.Sprintf("%s 当日下载量统计异常（%s）", emp.Name, day.In(loc).Format("01-02")),
			Summary: fmt.Sprintf("当日下载 %.0f 个，近 %d 天均值 %.2f、标准差 %.2f，z=%.2f > %.1f。",
				dec.Current, p.LookbackDays, dec.Mean, dec.Std, dec.Z, p.ZThreshold),
			DedupKey: dedup, Evidence: raw,
			WindowStart: &dayStart, WindowEnd: &dayEnd,
			EventTime: dayEnd.Add(-time.Second),
		}
		if err := e.upsertAlert(tx, in, now); err != nil {
			return err
		}
	}
	return nil
}
