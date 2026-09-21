package collector

import (
	"encoding/json"
	"fmt"
	"sort"

	"sitevitals/internal/models"
)

// Metric names.
const (
	MetricNavTTFB   = "nav_ttfb_ms"
	MetricNavLoad   = "nav_load_ms"
	MetricNavDCL    = "nav_dcl_ms"
	MetricNavDomC   = "nav_dom_complete_ms"
	MetricFCP       = "fcp_ms"
	MetricLCP       = "lcp_ms"
	MetricCLS       = "cls"
	MetricLongTasks = "long_tasks"
)

// longTaskWindow documents the observation window for long tasks.
type longTaskWindow struct {
	// WindowStartMS/WindowEndMS bound the observation interval relative to
	// navigation start (PerformanceObserver installed via addScriptToEvaluateOnNewDocument).
	WindowStartMS float64 `json:"window_start_ms"`
	WindowEndMS   float64 `json:"window_end_ms"`
	// ThresholdMS is the long-task definition: tasks longer than 50 ms.
	ThresholdMS float64 `json:"threshold_ms"`
	Count       int     `json:"count"`
	// TotalDurationMS sums every long task observed within the window.
	TotalDurationMS float64 `json:"total_duration_ms"`
	// MaxDurationMS is the single longest task.
	MaxDurationMS float64 `json:"max_duration_ms"`
	Tasks         []struct {
		Start float64 `json:"start_ms"`
		Dur   float64 `json:"duration_ms"`
	} `json:"tasks"`
}

func ms(v float64) *float64 { return &v }

// buildMetrics converts extracted page data into stored metric rows. Every
// metric always gets a row with an explicit status; nothing is zero-filled.
func buildMetrics(d *extractedData) []*models.Metric {
	out := []*models.Metric{}

	// --- Navigation timing ---
	if d.Nav != nil {
		n := d.Nav
		out = append(out,
			&models.Metric{Name: MetricNavTTFB, Status: models.MetricCollected, ValueMS: ms(n.TTFB)},
			&models.Metric{Name: MetricNavLoad, Status: models.MetricCollected, ValueMS: ms(n.Load)},
			&models.Metric{Name: MetricNavDCL, Status: models.MetricCollected, ValueMS: ms(n.DCL)},
			&models.Metric{Name: MetricNavDomC, Status: models.MetricCollected, ValueMS: ms(n.DomComplete)},
		)
	} else {
		msg := "performance NavigationTiming entry unavailable"
		out = append(out,
			&models.Metric{Name: MetricNavTTFB, Status: models.MetricUnsupported, Detail: msg},
			&models.Metric{Name: MetricNavLoad, Status: models.MetricUnsupported, Detail: msg},
			&models.Metric{Name: MetricNavDCL, Status: models.MetricUnsupported, Detail: msg},
			&models.Metric{Name: MetricNavDomC, Status: models.MetricUnsupported, Detail: msg},
		)
	}

	// --- FCP (from Paint Timing) ---
	if d.FCP != nil {
		out = append(out, &models.Metric{Name: MetricFCP, Status: models.MetricCollected, ValueMS: ms(*d.FCP)})
	} else {
		out = append(out, &models.Metric{
			Name:   MetricFCP,
			Status: models.MetricUnsupported,
			Detail: "no first-contentful-paint entry before extraction; unsupported or not yet painted",
		})
	}

	// --- LCP (buffered PerformanceObserver) ---
	switch {
	case d.LCP != nil:
		detail := ""
		b, _ := json.Marshal(map[string]any{
			"element": d.LCP.Element, "url_present": d.LCP.URL != "", "render_time_ms": d.LCP.RenderTime,
		})
		detail = string(b)
		out = append(out, &models.Metric{Name: MetricLCP, Status: models.MetricCollected, ValueMS: ms(d.LCP.Value), Detail: detail})
	case !d.LCPSupported:
		out = append(out, &models.Metric{
			Name:   MetricLCP,
			Status: models.MetricUnsupported,
			Detail: "largest-contentful-paint PerformanceObserver unsupported in this browser",
		})
	default:
		out = append(out, &models.Metric{
			Name:   MetricLCP,
			Status: models.MetricFailed,
			Detail: "observer supported but no LCP entry observed during the window",
		})
	}

	// --- CLS (cumulative across the whole run, no binned window) ---
	if d.CLSSupported {
		out = append(out, &models.Metric{
			Name:     MetricCLS,
			Status:   models.MetricCollected,
			ValueCLS: ms(d.CLS),
			Detail:   "cumulative layout shift from navigation start through settle window; shifts with hadRecentInput excluded",
		})
	} else {
		out = append(out, &models.Metric{
			Name:   MetricCLS,
			Status: models.MetricUnsupported,
			Detail: "layout-shift PerformanceObserver unsupported in this browser",
		})
	}

	// --- Long tasks (count + summary; window recorded explicitly) ---
	w := longTaskWindow{WindowStartMS: 0, WindowEndMS: d.Now, ThresholdMS: 50}
	if d.LTSupported {
		w.Count = len(d.LongTasks)
		tasks := make([]struct {
			Start float64 `json:"start_ms"`
			Dur   float64 `json:"duration_ms"`
		}, 0, len(d.LongTasks))
		for _, t := range d.LongTasks {
			tasks = append(tasks, struct {
				Start float64 `json:"start_ms"`
				Dur   float64 `json:"duration_ms"`
			}{Start: t.Start, Dur: t.Dur})
			w.TotalDurationMS += t.Dur
			if t.Dur > w.MaxDurationMS {
				w.MaxDurationMS = t.Dur
			}
		}
		sort.Slice(tasks, func(i, j int) bool { return tasks[i].Start < tasks[j].Start })
		w.Tasks = tasks
		b, _ := json.Marshal(w)
		out = append(out, &models.Metric{
			Name:   MetricLongTasks,
			Status: models.MetricCollected,
			Detail: string(b),
		})
	} else {
		b, _ := json.Marshal(w)
		out = append(out, &models.Metric{
			Name:   MetricLongTasks,
			Status: models.MetricUnsupported,
			Detail: fmt.Sprintf("longtask PerformanceObserver unsupported; window metadata: %s", string(b)),
		})
	}

	return out
}
