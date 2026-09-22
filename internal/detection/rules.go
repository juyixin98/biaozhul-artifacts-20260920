package detection

import (
	"encoding/json"
	"fmt"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/models"

	"gorm.io/gorm"
)

// RuleParams are the effective parameters of an active rule version.
type RuleParams struct {
	Code        string
	Version     uint32
	Enabled     bool
	Window      time.Duration // download_burst
	Threshold   int           // download_burst
	StartHour   int           // night_activity
	EndHour     int           // night_activity
	HistoryDays int           // statistical
	ZScore      float64       // statistical
	MinSamples  int           // statistical
}

// LoadRules reads the active rule versions. Disabled rules are returned with
// Enabled=false so callers know the current version (sweeps skip evaluation
// for disabled rules, and enabling bumps the version, triggering re-eval).
func LoadRules(cfg config.Config, db *gorm.DB) (map[string]RuleParams, error) {
	var rows []models.Rule
	if err := db.Where("active = ?", true).Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make(map[string]RuleParams, len(rows))
	for _, r := range rows {
		p := RuleParams{
			Code:    r.Code,
			Version: r.Version,
			Enabled: r.Enabled,
		}
		switch r.Code {
		case models.RuleDownloadBurst:
			p.Window = durParam(r.Params, "window_minutes", cfg.BurstWindow.Minutes())
			p.Threshold = intParam(r.Params, "threshold", cfg.BurstThreshold)
		case models.RuleNightActivity:
			p.StartHour = intParam(r.Params, "start_hour", cfg.NightStartHour)
			p.EndHour = intParam(r.Params, "end_hour", cfg.NightEndHour)
		case models.RuleStatistical:
			p.HistoryDays = intParam(r.Params, "history_days", cfg.StatHistoryDays)
			p.ZScore = floatParam(r.Params, "z_score", cfg.StatZScore)
			p.MinSamples = intParam(r.Params, "min_samples", cfg.StatMinSamples)
		case models.RuleFirstUSB:
			// no parameters
		}
		out[r.Code] = p
	}
	return out, nil
}

func intParam(m models.JSONMap, key string, def int) int {
	if m == nil {
		return def
	}
	switch v := m[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case json.Number:
		n, _ := v.Int64()
		return int(n)
	}
	return def
}

func floatParam(m models.JSONMap, key string, def float64) float64 {
	if m == nil {
		return def
	}
	switch v := m[key].(type) {
	case float64:
		return v
	case int:
		return float64(v)
	case json.Number:
		f, _ := v.Float64()
		return f
	}
	return def
}

func durParam(m models.JSONMap, key string, defMinutes float64) time.Duration {
	return time.Duration(floatParam(m, key, defMinutes) * float64(time.Minute))
}

// ValidateParams validates the params object for a rule code and returns a
// normalized copy.
func ValidateParams(code string, in models.JSONMap) (models.JSONMap, error) {
	if in == nil {
		in = models.JSONMap{}
	}
	out := models.JSONMap{}
	for k, v := range in {
		out[k] = v
	}
	switch code {
	case models.RuleDownloadBurst:
		w := floatParam(in, "window_minutes", 10)
		t := intParam(in, "threshold", 50)
		if w <= 0 || w > 1440 {
			return nil, fmt.Errorf("window_minutes must be in (0,1440]")
		}
		if t <= 0 {
			return nil, fmt.Errorf("threshold must be positive")
		}
		out["window_minutes"] = w
		out["threshold"] = t
	case models.RuleNightActivity:
		s := intParam(in, "start_hour", 20)
		e := intParam(in, "end_hour", 6)
		if s < 0 || s > 23 || e < 0 || e > 23 || s == e {
			return nil, fmt.Errorf("start_hour/end_hour must be in 0..23 and differ")
		}
		out["start_hour"] = s
		out["end_hour"] = e
	case models.RuleStatistical:
		h := intParam(in, "history_days", 30)
		z := floatParam(in, "z_score", 2.5)
		n := intParam(in, "min_samples", 10)
		if h < 2 || h > 365 {
			return nil, fmt.Errorf("history_days must be in [2,365]")
		}
		if z <= 0 {
			return nil, fmt.Errorf("z_score must be positive")
		}
		if n < 2 || n > h {
			return nil, fmt.Errorf("min_samples must be in [2,history_days]")
		}
		out["history_days"] = h
		out["z_score"] = z
		out["min_samples"] = n
	case models.RuleFirstUSB:
		// no parameters
	default:
		return nil, fmt.Errorf("unknown rule code %q", code)
	}
	return out, nil
}
