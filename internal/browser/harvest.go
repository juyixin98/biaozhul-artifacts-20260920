package browser

import (
	"context"
	"encoding/json"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/chromedp"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
)

// metricsObserverScript is injected before any page script runs (via
// Page.addScriptToEvaluateOnNewDocument). Buffered observers let us capture
// FCP/LCP/layout shifts that happen before harvest. Long tasks can only be
// observed while this observer is attached — see the run's window metadata.
const metricsObserverScript = `
(function () {
  if (window.__sv) return;
  var store = {
    nav: null, fcp: null, lcp: null, cls: 0, lcpSupported: false,
    clsSupported: false, ltSupported: false, fcpSupported: false,
    longTasks: [], resources: [], bufferFull: false
  };
  window.__sv = store;
  try {
    if (typeof PerformanceObserver === 'function') {
      try {
        var lt = new PerformanceObserver(function (list) {
          list.getEntries().forEach(function (e) {
            store.longTasks.push({ start: e.startTime, duration: e.duration });
          });
        });
        lt.observe({ entryTypes: ['longtask'] });
        store.ltSupported = true;
      } catch (e) { store.ltSupported = false; }

      try {
        var lcp = new PerformanceObserver(function (list) {
          var entries = list.getEntries();
          if (entries.length) {
            store.lcp = entries[entries.length - 1].startTime;
          }
        });
        lcp.observe({ type: 'largest-contentful-paint', buffered: true });
        store.lcpSupported = true;
      } catch (e) { store.lcpSupported = false; }

      try {
        var clsValue = 0;
        var clsObserver = new PerformanceObserver(function (list) {
          list.getEntries().forEach(function (entry) {
            if (!entry.hadRecentInput) clsValue += entry.value;
          });
          store.cls = clsValue;
        });
        clsObserver.observe({ type: 'layout-shift', buffered: true });
        store.clsSupported = true;
      } catch (e) { store.clsSupported = false; }

      try {
        var fcpObserver = new PerformanceObserver(function (list) {
          var entries = list.getEntries();
          if (entries.length) store.fcp = entries[entries.length - 1].startTime;
        });
        fcpObserver.observe({ type: 'paint', buffered: true });
        store.fcpSupported = true;
      } catch (e) { store.fcpSupported = false; }

      try {
        // A large buffer keeps the full waterfall on resource-heavy pages.
        if (performance.setResourceTimingBufferSize) {
          performance.setResourceTimingBufferSize(5000);
        }
        performance.addEventListener && performance.addEventListener('resourcetimingbufferfull', function () {
          store.bufferFull = true;
          performance.setResourceTimingBufferSize && performance.setResourceTimingBufferSize(10000);
        });
      } catch (e) {}
    }
  } catch (e) { store.installError = String(e); }
})();
`

const harvestScript = `
(function () {
  var s = window.__sv || {};
  var out = {
    installError: s.installError || null,
    fcp: s.fcp == null ? null : s.fcp,
    lcp: s.lcp == null ? null : s.lcp,
    cls: s.cls || 0,
    fcpSupported: !!s.fcpSupported,
    lcpSupported: !!s.lcpSupported,
    clsSupported: !!s.clsSupported,
    ltSupported: !!s.ltSupported,
    longTasks: s.longTasks || [],
    bufferFull: !!s.bufferFull,
    nav: null,
    resources: []
  };
  try {
    var navs = performance.getEntriesByType('navigation');
    if (navs && navs.length) {
      var n = navs[0];
      out.nav = {
        startTime: n.startTime,
        domContentLoaded: n.domContentLoadedEventEnd,
        loadEventEnd: n.loadEventEnd,
        responseEnd: n.responseEnd,
        duration: n.duration,
        transferSize: n.transferSize,
        timingRestricted: n.domainLookupStart === 0 && n.connectStart === 0 && n.requestStart === 0
      };
    }
  } catch (e) { out.navError = String(e); }
  try {
    out.resources = performance.getEntriesByType('resource').map(function (r) {
      return {
        name: r.name,
        initiatorType: r.initiatorType,
        startTime: r.startTime,
        duration: r.duration,
        transferSize: r.transferSize,
        decodedBodySize: r.decodedBodySize,
        restricted: r.domainLookupStart === 0 && r.connectStart === 0 && r.requestStart === 0
          && (r.transferSize === undefined || r.transferSize === 0)
      };
    });
  } catch (e) { out.resourceError = String(e); }
  return out;
})();
`

// jsResource is one resource timing entry reported by the page.
type jsResource struct {
	Name            string  `json:"name"`
	InitiatorType   string  `json:"initiatorType"`
	StartTime       float64 `json:"startTime"`
	Duration        float64 `json:"duration"`
	TransferSize    float64 `json:"transferSize"`
	DecodedBodySize float64 `json:"decodedBodySize"`
	Restricted      bool    `json:"restricted"`
}

type jsLongTask struct {
	Start    float64 `json:"start"`
	Duration float64 `json:"duration"`
}

type jsNav struct {
	StartTime        float64 `json:"startTime"`
	DomContentLoaded float64 `json:"domContentLoaded"`
	LoadEventEnd     float64 `json:"loadEventEnd"`
	ResponseEnd      float64 `json:"responseEnd"`
	Duration         float64 `json:"duration"`
	TransferSize     float64 `json:"transferSize"`
	TimingRestricted bool    `json:"timingRestricted"`
}

type harvested struct {
	InstallError  string       `json:"installError"`
	FCP           *float64     `json:"fcp"`
	LCP           *float64     `json:"lcp"`
	CLS           float64      `json:"cls"`
	FCPSupported  bool         `json:"fcpSupported"`
	LCPSupported  bool         `json:"lcpSupported"`
	CLSSupported  bool         `json:"clsSupported"`
	LTSupported   bool         `json:"ltSupported"`
	LongTasks     []jsLongTask `json:"longTasks"`
	BufferFull    bool         `json:"bufferFull"`
	Nav           *jsNav       `json:"nav"`
	NavError      string       `json:"navError"`
	Resources     []jsResource `json:"resources"`
	ResourceError string       `json:"resourceError"`

	jsResources []jsResource
	NavBaseMono float64
}

// harvest evaluates the metrics gatherer inside the page.
func harvest(ctx context.Context) (*harvested, error) {
	var raw string
	evalCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	err := chromedp.Run(evalCtx, chromedp.ActionFunc(func(ctx context.Context) error {
		v, exp, e := runtime.Evaluate(harvestScript).WithReturnByValue(true).WithAwaitPromise(true).Do(ctx)
		if e != nil {
			return e
		}
		if exp != nil {
			return fmt.Errorf("harvest exception: %s", exp.Text)
		}
		if v == nil || v.Value == nil {
			return nil
		}
		raw = string(v.Value)
		return nil
	}))
	if err != nil {
		return nil, err
	}
	var h harvested
	if err := json.Unmarshal([]byte(raw), &h); err != nil {
		return nil, err
	}
	h.jsResources = h.Resources
	return &h, nil
}

// atomicBool is a small helper for cross-goroutine doc-blocked flags.
type atomicBool struct{ v int32 }

func (a *atomicBool) set(b bool) {
	if b {
		atomic.StoreInt32(&a.v, 1)
	} else {
		atomic.StoreInt32(&a.v, 0)
	}
}

func (a *atomicBool) Load() bool { return atomic.LoadInt32(&a.v) == 1 }

// fillRunMetrics maps harvested in-page metrics + CDP waterfall into the
// persisted RunMetrics, marking every unavailable metric explicitly.
func fillRunMetrics(h *harvested, resources []models.Resource, d *Diagnostics) *store.RunMetrics {
	m := &store.RunMetrics{
		MetricStatus: models.MetricSet{
			Navigation: models.MetricUnsupported,
			FCP:        models.MetricUnsupported,
			LCP:        models.MetricUnsupported,
			CLS:        models.MetricUnsupported,
			LongTasks:  models.MetricUnsupported,
			Resources:  models.MetricOK,
		},
		WindowStartOffsetMS: 0,
	}

	// Navigation duration: prefer Navigation Timing L2 duration/loadEventEnd.
	if h.Nav != nil {
		if h.Nav.LoadEventEnd > 0 {
			v := h.Nav.LoadEventEnd - h.Nav.StartTime
			m.NavDurationMS = &v
			m.MetricStatus.Navigation = models.MetricOK
		} else if h.Nav.Duration > 0 {
			v := h.Nav.Duration
			m.NavDurationMS = &v
			m.MetricStatus.Navigation = models.MetricPartial
			d.Warnings = append(d.Warnings, "loadEventEnd unavailable; navigation duration is partial (harvested before load settled)")
		} else if h.Nav.TimingRestricted {
			m.MetricStatus.Navigation = models.MetricPartial
		}
	} else if h.NavError != "" {
		m.MetricStatus.Navigation = models.MetricFailed
		d.Warnings = append(d.Warnings, "navigation timing error: "+h.NavError)
	}

	// FCP
	if h.FCP != nil {
		v := *h.FCP
		m.FCPMS = &v
		m.MetricStatus.FCP = models.MetricOK
	} else if !h.FCPSupported {
		m.MetricStatus.FCP = models.MetricUnsupported
	} else {
		m.MetricStatus.FCP = models.MetricFailed
		d.Warnings = append(d.Warnings, "FCP observer supported but no paint entry observed before harvest")
	}

	// LCP
	if h.LCP != nil {
		v := *h.LCP
		m.LCPMS = &v
		m.MetricStatus.LCP = models.MetricOK
	} else if !h.LCPSupported {
		m.MetricStatus.LCP = models.MetricUnsupported
	} else {
		m.MetricStatus.LCP = models.MetricFailed
		d.Warnings = append(d.Warnings, "LCP observer supported but no candidate observed before harvest")
	}

	// CLS: 0 is a legitimate value, so distinguish "measured 0" via support.
	if h.CLSSupported {
		v := h.CLS
		m.CLS = &v
		m.MetricStatus.CLS = models.MetricOK
	} else {
		m.MetricStatus.CLS = models.MetricUnsupported
	}

	// Long tasks
	if h.LTSupported {
		n := len(h.LongTasks)
		m.LongTaskCount = &n
		var total, max float64
		for _, t := range h.LongTasks {
			total += t.Duration
			if t.Duration > max {
				max = t.Duration
			}
		}
		m.LongTaskTotalMS = &total
		m.LongTaskMaxMS = &max
		m.MetricStatus.LongTasks = models.MetricOK
	} else {
		m.MetricStatus.LongTasks = models.MetricUnsupported
	}

	if h.InstallError != "" {
		d.Warnings = append(d.Warnings, "metrics observer install error: "+h.InstallError)
	}
	if h.BufferFull {
		d.Warnings = append(d.Warnings, "resource timing buffer filled during load; waterfall may omit late resources")
		m.MetricStatus.Resources = models.MetricPartial
	}
	if h.ResourceError != "" {
		m.MetricStatus.Resources = models.MetricPartial
		d.Warnings = append(d.Warnings, "resource timing read error: "+h.ResourceError)
	}
	m.Resources = resources
	m.Warnings = d.Warnings
	m.Redirects = d.Redirects
	m.Violations = d.Violations
	return m
}
