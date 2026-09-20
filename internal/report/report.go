package report

import (
	"fmt"
	"strings"
	"time"

	"sitevitals/internal/models"
)

// Build renders the markdown report for a successful run. Missing metrics are
// printed as their explicit status ("unsupported"/"failed"/"partial") rather
// than 0.
func Build(task *models.Task, r *models.Run, alerts []models.BudgetAlert) string {
	var b strings.Builder
	fmt.Fprintf(&b, "# SiteVitals report — task %d\n\n", task.ID)
	fmt.Fprintf(&b, "- URL: `%s`\n", r.URL)
	if r.FinalURL != "" && r.FinalURL != r.URL {
		fmt.Fprintf(&b, "- Final URL after redirects: `%s`\n", r.FinalURL)
	}
	fmt.Fprintf(&b, "- Viewport: **%s**\n", r.Viewport)
	fmt.Fprintf(&b, "- Attempt: %d\n", r.AttemptNo)
	fmt.Fprintf(&b, "- Started: %s\n", r.StartedAt.UTC().Format(time.RFC3339))
	if r.FinishedAt != nil {
		fmt.Fprintf(&b, "- Finished: %s\n", r.FinishedAt.UTC().Format(time.RFC3339))
	}
	b.WriteString("\n## Core metrics\n\n")
	fmt.Fprintf(&b, "| Metric | Value | Status |\n|---|---:|---|\n")
	fmt.Fprintf(&b, "| Navigation duration | %s | %s |\n", msOrNA(r.NavDurationMS), r.MetricStatus.Navigation)
	fmt.Fprintf(&b, "| First Contentful Paint | %s | %s |\n", msOrNA(r.FCPMS), r.MetricStatus.FCP)
	fmt.Fprintf(&b, "| Largest Contentful Paint | %s | %s |\n", msOrNA(r.LCPMS), r.MetricStatus.LCP)
	fmt.Fprintf(&b, "| Cumulative Layout Shift | %s | %s |\n", clsOrNA(r.CLS), r.MetricStatus.CLS)
	fmt.Fprintf(&b, "| Long tasks (count) | %s | %s |\n", intOrNA(r.LongTaskCount), r.MetricStatus.LongTasks)
	fmt.Fprintf(&b, "| Long tasks total | %s | %s |\n", msOrNA(r.LongTaskTotalMS), r.MetricStatus.LongTasks)
	fmt.Fprintf(&b, "| Long tasks max | %s | %s |\n", msOrNA(r.LongTaskMaxMS), r.MetricStatus.LongTasks)

	b.WriteString("\n### Long task observation window\n\n")
	if r.WindowEnd != nil {
		fmt.Fprintf(&b, "Long tasks were observed by a PerformanceObserver installed before any page script ran (offset %.0f ms from navigationStart) until harvest at %s. Tasks are tasks with duration > 50 ms on the main frame per the Long Tasks API; cross-origin iframes are not attributable and tasks occurring after harvest are not counted.\n\n",
			r.WindowStartOffsetMS, r.WindowEnd.UTC().Format(time.RFC3339))
	} else {
		b.WriteString("Long task window unknown (metric not collected).\n\n")
	}

	if len(alerts) > 0 {
		b.WriteString("## Budget alerts\n\n| Metric | Actual | Threshold | Severity |\n|---|---:|---:|---|\n")
		for _, a := range alerts {
			fmt.Fprintf(&b, "| %s | %.3f | %.3f | %s |\n", a.Metric, a.Actual, a.Threshold, a.Severity)
		}
		b.WriteString("\n")
	}

	if len(r.Redirects) > 0 {
		b.WriteString("## Redirect chain\n\n")
		for i, h := range r.Redirects {
			flag := ""
			if h.Blocked {
				flag = " ⛔ blocked"
			} else if !h.Whitelisted {
				flag = " ⚠ off-whitelist"
			}

			if h.Status > 0 {
				fmt.Fprintf(&b, "%d. `%s` → %d%s\n", i+1, h.URL, h.Status, flag)
			} else {
				fmt.Fprintf(&b, "%d. `%s`%s\n", i+1, h.URL, flag)
			}
		}
		b.WriteString("\n")
	}

	if len(r.Violations) > 0 {
		b.WriteString("## Whitelist / protocol violations\n\n| URL | Kind | Stage | Blocked |\n|---|---|---|---|\n")
		for _, v := range r.Violations {
			fmt.Fprintf(&b, "| `%s` | %s | %s | %t |\n", v.URL, v.Kind, v.Stage, v.Blocked)
		}
		b.WriteString("\n")
	}

	if len(r.Warnings) > 0 {
		b.WriteString("## Warnings\n\n")
		for _, w := range r.Warnings {
			fmt.Fprintf(&b, "- %s\n", w)
		}
		b.WriteString("\n")
	}

	if len(r.Resources) > 0 {
		fmt.Fprintf(&b, "## Resource waterfall (%d resources)\n\n", len(r.Resources))
		b.WriteString("| # | Resource | Type | Status | Start (ms) | Duration (ms) | Notes |\n|---:|---|---|---:|---:|---:|---|\n")
		for i, res := range r.Resources {
			notes := ""
			if res.Failed {
				notes = "FAILED: " + res.FailureText
			} else if res.Blocked {
				notes = "blocked by policy"
			} else if res.TimingRestricted {
				notes = "timing restricted (cross-origin)"
			} else if res.FromCache {
				notes = "served from cache"
			}
			status := ""
			if res.Status > 0 {
				status = fmt.Sprintf("%d", res.Status)
			} else {
				status = "—"
			}
			fmt.Fprintf(&b, "| %d | `%s` | %s | %s | %.1f | %.1f | %s |\n",
				i+1, truncateURL(res.URL), res.Type, status, res.StartMS, res.DurationMS, notes)
		}
		b.WriteString("\n")
	}

	b.WriteString("## Metric status legend\n\n")
	b.WriteString("- `ok`: measured via the real browser CDP/Performance API.\n")
	b.WriteString("- `unsupported`: the browser did not expose the metric.\n")
	b.WriteString("- `partial`: metric available but timing detail restricted (e.g. cross-origin without Timing-Allow-Origin).\n")
	b.WriteString("- `failed`: collection attempted but failed; the value is NULL, never zero.\n")
	return b.String()
}
func msOrNA(v *float64) string {
	if v == nil {
		return "N/A"
	}
	return fmt.Sprintf("%.1f ms", *v)
}

func clsOrNA(v *float64) string {
	if v == nil {
		return "N/A"
	}
	return fmt.Sprintf("%.4f", *v)
}

func intOrNA(v *int) string {
	if v == nil {
		return "N/A"
	}
	return fmt.Sprintf("%d", *v)
}

func truncateURL(u string) string {
	if len(u) > 90 {
		return u[:87] + "..."
	}
	return u
}
