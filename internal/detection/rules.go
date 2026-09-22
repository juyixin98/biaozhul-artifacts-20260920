package detection

import (
	"encoding/json"
	"fmt"

	"activityguard/internal/models"

	"gorm.io/gorm"
)

// ActiveRules 是当前生效的规则集合（每个 rule_key 一条最高版本的激活规则）。
type ActiveRules struct {
	byKey map[string]models.DetectionRule
}

// BurstParams 10 分钟大量下载参数。
type BurstParams struct {
	WindowMinutes int `json:"window_minutes"`
	Threshold     int `json:"threshold"` // count > threshold 触发
}

// NightParams 夜间时段参数，按员工本地时区解释，[StartHour, EndHour) 左闭右开。
// StartHour=20, EndHour=6 表示 20:00 至次日 06:00。
type NightParams struct {
	StartHour int `json:"start_hour"`
	EndHour   int `json:"end_hour"`
}

// ZScoreParams 统计异常参数。
type ZScoreParams struct {
	LookbackDays    int     `json:"lookback_days"`
	ZThreshold      float64 `json:"z_threshold"`
	MinSampleDays   int     `json:"min_sample_days"`   // 至少有多少天样本（窗口内天数）
	MinPositiveDays int     `json:"min_positive_days"` // 至少多少天出现过事件（否则样本不足）
	Metric          string  `json:"metric"`
}

func loadActiveRules(gdb *gorm.DB) (*ActiveRules, error) {
	var rows []models.DetectionRule
	if err := gdb.Where("is_active = ?", true).Find(&rows).Error; err != nil {
		return nil, err
	}
	out := &ActiveRules{byKey: make(map[string]models.DetectionRule, len(rows))}
	for _, r := range rows {
		if cur, ok := out.byKey[r.RuleKey]; !ok || r.Version > cur.Version {
			out.byKey[r.RuleKey] = r
		}
	}
	return out, nil
}

func (a *ActiveRules) get(key string) (models.DetectionRule, bool) {
	r, ok := a.byKey[key]
	return r, ok
}

func decodeParams[T any](r models.DetectionRule) (T, error) {
	var p T
	if err := json.Unmarshal(r.Params, &p); err != nil {
		return p, fmt.Errorf("rule %s v%d params: %w", r.RuleKey, r.Version, err)
	}
	return p, nil
}

func burstDefaults(p BurstParams) BurstParams {
	if p.WindowMinutes <= 0 {
		p.WindowMinutes = 10
	}
	if p.Threshold <= 0 {
		p.Threshold = 50
	}
	return p
}

func nightDefaults(p NightParams) NightParams {
	if p.StartHour == 0 && p.EndHour == 0 {
		p.StartHour, p.EndHour = 20, 6
	}
	return p
}

func zDefaults(p ZScoreParams) ZScoreParams {
	if p.LookbackDays <= 0 {
		p.LookbackDays = 30
	}
	if p.ZThreshold <= 0 {
		p.ZThreshold = 2.5
	}
	if p.MinSampleDays <= 0 {
		p.MinSampleDays = 7
	}
	if p.MinPositiveDays <= 0 {
		p.MinPositiveDays = 5
	}
	if p.Metric == "" {
		p.Metric = "file_download_daily"
	}
	return p
}
