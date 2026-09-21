// Package collector drives a local headless Chromium over the Chrome DevTools
// Protocol and captures real performance data: navigation timing, FCP, LCP,
// CLS, long tasks and the resource waterfall.
//
// Honesty rules:
//   - A metric that is unavailable or could not be read is stored with status
//     "unsupported" or "failed"; it is never replaced with a zero.
//   - Navigation timeout, browser exit and partial subresource failure are
//     reported as distinct outcomes.
package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/emulation"
	"github.com/chromedp/cdproto/fetch"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/chromedp"

	"sitevitals/internal/models"
	"sitevitals/internal/policy"
)

// Viewport presets (width x height @ scale).
type Preset struct {
	Width  int64
	Height int64
	Scale  float64
	Mobile bool
}

// Presets maps the three supported viewport names to device metrics.
var Presets = map[string]Preset{
	models.ViewportMobile:  {Width: 390, Height: 844, Scale: 3, Mobile: true},
	models.ViewportTablet:  {Width: 820, Height: 1180, Scale: 2, Mobile: false},
	models.ViewportDesktop: {Width: 1366, Height: 768, Scale: 1, Mobile: false},
}

// Options configures one collection.
type Options struct {
	ExecPath     string
	Viewport     string
	Checker      *policy.Checker
	MaxRedirects int
	NavTimeout   time.Duration
	// SettleDelay is the quiet observation window after load, during which
	// late LCP candidates, layout shifts and long tasks are still recorded.
	SettleDelay time.Duration
	// CrashAfterLoad crashes the browser (browser.crash) once load completes.
	// It exists for the crash-recovery test and must never be set in production.
	CrashAfterLoad bool
}

// CollectError classifies a collection failure.
type CollectError struct {
	Class   string // models.Fail* constant
	Message string
}

func (e *CollectError) Error() string { return e.Class + ": " + e.Message }

// Result is what a successful collection returns.
type Result struct {
	FinalURL         string
	RedirectHops     int
	Metrics          []*models.Metric
	Resources        []*models.ResourceEntry
	Events           []*models.RunEvent
	ResourceFailures int
	BlockedResources int
}

// bootstrapJS installs PerformanceObservers before any page script runs.
const bootstrapJS = `
(function () {
  window.__sv = { errors: [], lcp: null, lcpSupported: false, clsSupported: false,
                   cls: 0, ltSupported: false, longTasks: [] };
  try {
    new PerformanceObserver(function (l) {
      var es = l.getEntries();
      for (var i = 0; i < es.length; i++) { window.__sv.lcp = es[i]; }
    }).observe({ type: 'largest-contentful-paint', buffered: true });
    window.__sv.lcpSupported = true;
  } catch (e) { window.__sv.errors.push('lcp:' + e.message); }
  try {
    new PerformanceObserver(function (l) {
      var es = l.getEntries();
      for (var i = 0; i < es.length; i++) {
        if (!es[i].hadRecentInput) { window.__sv.cls += es[i].value; }
      }
    }).observe({ type: 'layout-shift', buffered: true, durationThreshold: 0 });
    window.__sv.clsSupported = true;
  } catch (e) { window.__sv.errors.push('cls:' + e.message); }
  try {
    new PerformanceObserver(function (l) {
      var es = l.getEntries();
      for (var i = 0; i < es.length; i++) {
        window.__sv.longTasks.push({ start: es[i].startTime, dur: es[i].duration });
      }
    }).observe({ entryTypes: ['longtask'], buffered: true });
    window.__sv.ltSupported = true;
  } catch (e) { window.__sv.errors.push('lt:' + e.message); }
})();
`

// extractJS reads back the buffered performance data.
const extractJS = `
(function () {
  var out = { errors: window.__sv.errors, lcpSupported: window.__sv.lcpSupported,
              clsSupported: window.__sv.clsSupported, ltSupported: window.__sv.ltSupported,
              cls: window.__sv.cls, longTasks: window.__sv.longTasks,
              nav: null, fcp: null, lcp: null, location: location.href, now: performance.now() };
  try {
    var navs = performance.getEntriesByType('navigation');
    if (navs && navs.length) {
      var n = navs[0];
      out.nav = { start: 0, ttfb: n.responseStart, dcl: n.domContentLoadedEventEnd,
                  load: n.loadEventEnd, domComplete: n.domComplete,
                  transferSize: n.transferSize, encodedBodySize: n.encodedBodySize };
    }
    var paints = performance.getEntriesByType('paint');
    for (var i = 0; i < paints.length; i++) {
      if (paints[i].name === 'first-contentful-paint') { out.fcp = paints[i].startTime; }
    }
    if (window.__sv.lcp) {
      out.lcp = { value: window.__sv.lcp.startTime,
                  renderTime: window.__sv.lcp.renderTime,
                  element: (window.__sv.lcp.element && window.__sv.lcp.element.tagName) || '',
                  url: window.__sv.lcp.url || '' };
    }
  } catch (e) { out.errors.push('extract:' + e.message); }
  return JSON.stringify(out);
})();
`

type extractedData struct {
	Errors       []string `json:"errors"`
	LCPSupported bool     `json:"lcpSupported"`
	CLSSupported bool     `json:"clsSupported"`
	LTSupported  bool     `json:"ltSupported"`
	CLS          float64  `json:"cls"`
	LongTasks    []struct {
		Start float64 `json:"start"`
		Dur   float64 `json:"dur"`
	} `json:"longTasks"`
	Nav *struct {
		TTFB         float64 `json:"ttfb"`
		DCL          float64 `json:"dcl"`
		Load         float64 `json:"load"`
		DomComplete  float64 `json:"domComplete"`
		TransferSize float64 `json:"transferSize"`
	} `json:"nav"`
	FCP *float64 `json:"fcp"`
	LCP *struct {
		Value      float64 `json:"value"`
		RenderTime float64 `json:"renderTime"`
		Element    string  `json:"element"`
		URL        string  `json:"url"`
	} `json:"lcp"`
	Location string  `json:"location"`
	Now      float64 `json:"now"`
}

// resRow tracks one network request across CDP events. Timestamps are network
// monotonic times (an arbitrary but shared base); deltas against the first
// document request give waterfall offsets.
type resRow struct {
	url        string
	rtype      string
	initiator  string
	httpStatus int
	start      time.Time
	end        time.Time
	bytes      int64
	gotStart   bool
	gotFinish  bool
	blocked    bool
	failed     bool
	failText   string
	isDocument bool
}

func monoMS(base, t time.Time) float64 {
	return float64(t.Sub(base).Microseconds()) / 1000.0
}

// Collect runs one navigation + measurement and returns the populated Result,
// or a *CollectError. The browser process is always released before returning.
func Collect(ctx context.Context, targetURL string, opt Options) (*Result, error) {
	preset, ok := Presets[opt.Viewport]
	if !ok {
		return nil, &CollectError{Class: models.FailCollector, Message: "unknown viewport: " + opt.Viewport}
	}
	if opt.Checker == nil {
		return nil, &CollectError{Class: models.FailCollector, Message: "missing allow-list checker"}
	}
	// Defense in depth: the enqueue API checks this too.
	if v := opt.Checker.Check(targetURL); v != nil {
		return nil, &CollectError{Class: models.FailPolicy, Message: v.Error()}
	}
	if opt.NavTimeout <= 0 {
		opt.NavTimeout = 45 * time.Second
	}
	if opt.SettleDelay <= 0 {
		opt.SettleDelay = 2 * time.Second
	}
	if opt.MaxRedirects <= 0 {
		opt.MaxRedirects = 5
	}

	chromePath := opt.ExecPath
	if chromePath == "" {
		var err error
		chromePath, err = findChrome()
		if err != nil {
			return nil, &CollectError{Class: models.FailBrowserExited, Message: err.Error()}
		}
	}

	userDir, err := os.MkdirTemp("", "sitevitals-chrome-")
	if err != nil {
		return nil, &CollectError{Class: models.FailCollector, Message: "user-data-dir: " + err.Error()}
	}
	defer os.RemoveAll(userDir)

	portFile := filepath.Join(userDir, "DevToolsActivePort")
	args := []string{
		"--remote-debugging-port=0",
		"--headless=new",
		"--no-sandbox",
		"--disable-gpu",
		"--disable-dev-shm-usage",
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-extensions",
		"--disable-background-networking",
		"--disable-sync",
		"--mute-audio",
		"--hide-scrollbars",
		"--user-data-dir=" + userDir,
		"about:blank",
	}
	cmd := exec.CommandContext(ctx, chromePath, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// Kill the whole process group so utility/renderer children cannot leak.
	cmd.Cancel = func() error {
		if p := cmd.Process; p != nil {
			_ = syscall.Kill(-p.Pid, syscall.SIGKILL)
		}
		return os.ErrProcessDone
	}
	cmd.WaitDelay = 3 * time.Second
	if err := cmd.Start(); err != nil {
		return nil, &CollectError{Class: models.FailBrowserExited, Message: "start chromium: " + err.Error()}
	}
	exitedCh := make(chan struct{})
	var waitErr error
	go func() { waitErr = cmd.Wait(); close(exitedCh) }()

	// Guaranteed teardown: cancel CDP contexts and kill the browser group.
	var cancelAlloc, cancelBrowser, cancelTab context.CancelFunc
	defer func() {
		if cancelTab != nil {
			cancelTab()
		}
		if cancelBrowser != nil {
			cancelBrowser()
		}
		if cancelAlloc != nil {
			cancelAlloc()
		}
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		select {
		case <-exitedCh:
		case <-time.After(4 * time.Second):
		}
	}()

	browserExited := func() bool {
		select {
		case <-exitedCh:
			return true
		default:
			return false
		}
	}

	port, err := waitForDevToolsPort(portFile, exitedCh, 15*time.Second)
	if err != nil {
		return nil, &CollectError{Class: models.FailBrowserExited, Message: err.Error()}
	}
	devtoolsURL := "http://127.0.0.1:" + port

	allocCtx, allocCancel := chromedp.NewRemoteAllocator(context.Background(), devtoolsURL)
	cancelAlloc = allocCancel
	browserCtx, bCancel := chromedp.NewContext(allocCtx)
	cancelBrowser = bCancel
	if err := chromedp.Run(browserCtx); err != nil {
		if browserExited() {
			return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited during startup: " + err.Error()}
		}
		return nil, &CollectError{Class: models.FailCollector, Message: "connect CDP: " + err.Error()}
	}
	tabCtx, tCancel := chromedp.NewContext(browserCtx)
	cancelTab = tCancel
	// cmdCtx dispatches commands directly to the tab target; it is populated
	// after the setup actions attach the target. Using tabCtx synchronously
	// inside a ListenTarget callback deadlocks the event-dispatch loop.
	var cmdCtx context.Context

	// Per-navigation shared state.
	var (
		mu            sync.Mutex
		rows          = map[network.RequestID]*resRow{}
		blockedIDs    = map[network.RequestID]bool{}
		navStartMono  time.Time
		navStartSet   bool
		guard         = policy.NewRedirectGuard(opt.MaxRedirects)
		loadCh        = make(chan struct{}, 1)
		policyErr     *policy.Violation
		policyErrSet  bool
		events        []*models.RunEvent
		resourceFails int
		blockedCount  int
	)
	addEvent := func(kind, msg string) {
		mu.Lock()
		events = append(events, &models.RunEvent{Kind: kind, Message: trunc(msg, 1000)})
		mu.Unlock()
	}
	setPolicyErr := func(v *policy.Violation) {
		mu.Lock()
		if !policyErrSet {
			policyErr, policyErrSet = v, true
		}
		mu.Unlock()
	}

	chromedp.ListenTarget(tabCtx, func(ev interface{}) {
		switch e := ev.(type) {
		case *fetch.EventRequestPaused:
			// Dispatch asynchronously so a slow fetch.* command never blocks
			// event delivery. Commands use cmdCtx (bound directly to the
			// Target executor); tabCtx here would deadlock the dispatch loop.
			go func() {
				actCtx := cmdCtx
				if actCtx == nil {
					return
				}
				handlePaused(actCtx, e, opt.Checker, guard, &mu, rows, blockedIDs,
					addEvent, setPolicyErr,
					func() { mu.Lock(); blockedCount++; mu.Unlock() })
			}()

		case *network.EventRequestWillBeSent:
			mu.Lock()
			rid := network.RequestID(e.RequestID)
			row := rows[rid]
			if row == nil {
				row = &resRow{url: e.Request.URL, initiator: string(e.Initiator.Type)}
				rows[rid] = row
			} else if e.RedirectResponse == nil {
				row.url = e.Request.URL
			}
			// The first "other"-initiator request is the browser-initiated
			// main document navigation; resource type arrives with ResponseReceived.
			isDoc := !navStartSet && string(e.Initiator.Type) == "other"
			if isDoc {
				row.isDocument = true
				row.rtype = "Document"
				if e.Timestamp != nil {
					navStartMono = time.Time(*e.Timestamp)
					navStartSet = true
				}
			}
			if !row.gotStart && e.Timestamp != nil {
				row.start = time.Time(*e.Timestamp)
				row.gotStart = true
			}
			mu.Unlock()

		case *network.EventResponseReceived:
			mu.Lock()
			if row := rows[network.RequestID(e.RequestID)]; row != nil {
				row.httpStatus = int(e.Response.Status)
				if row.rtype == "" {
					row.rtype = string(e.Type)
				}
				if e.Type == network.ResourceTypeDocument {
					row.isDocument = true
				}
			}
			mu.Unlock()

		case *network.EventLoadingFinished:
			mu.Lock()
			if row := rows[network.RequestID(e.RequestID)]; row != nil {
				if e.Timestamp != nil {
					row.end = time.Time(*e.Timestamp)
					row.gotFinish = true
				}
				row.bytes = int64(e.EncodedDataLength)
				// An HTTP 4xx/5xx is a delivered response but a failed
				// resource: record it as a partial failure without failing run.
				if !row.isDocument && !row.blocked && row.httpStatus >= 400 {
					if !row.failed {
						row.failed = true
						row.failText = fmt.Sprintf("HTTP %d", row.httpStatus)
						resourceFails++
					}
				}
			}
			mu.Unlock()

		case *network.EventLoadingFailed:
			mu.Lock()
			rid := network.RequestID(e.RequestID)
			if row := rows[rid]; row != nil {
				if blockedIDs[rid] {
					row.blocked = true
				} else {
					row.failed = true
					row.failText = e.ErrorText
					if !row.isDocument {
						resourceFails++
					}
				}
				if e.Timestamp != nil {
					row.end = time.Time(*e.Timestamp)
				}
			}
			mu.Unlock()

		case *page.EventLoadEventFired:
			select {
			case loadCh <- struct{}{}:
			default:
			}
		}
	})

	// Enable domains and configure the tab before navigating. Intercept every
	// request at the Request stage only; redirect chains are tracked there via
	// RedirectedRequestID (see handlePaused).
	setupActions := []chromedp.Action{
		network.Enable(),
		fetch.Enable().WithPatterns([]*fetch.RequestPattern{{
			URLPattern:   "*",
			RequestStage: fetch.RequestStageRequest,
		}}),
		page.Enable(),
		chromedp.ActionFunc(func(c context.Context) error {
			_, err := page.AddScriptToEvaluateOnNewDocument(bootstrapJS).Do(c)
			return err
		}),
		emulation.SetDeviceMetricsOverride(preset.Width, preset.Height, preset.Scale, preset.Mobile),
	}
	if err := chromedp.Run(tabCtx, setupActions...); err != nil {
		if browserExited() {
			return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited during setup: " + err.Error()}
		}
		return nil, &CollectError{Class: models.FailCollector, Message: "CDP setup: " + err.Error()}
	}

	// Commands issued from within a ListenTarget callback need an executor
	// that bypasses the context's frame action queue: a callback parked on
	// that queue would deadlock target event dispatch. cdp.WithExecutor
	// binds directly to this tab's Target.
	fromTab := chromedp.FromContext(tabCtx)
	cmdCtx = cdp.WithExecutor(context.Background(), fromTab.Target)

	// Hard deadline for the whole navigation (command + load event). The
	// deadline context guarantees page.Navigate itself cannot block forever
	// when the server holds the connection open without ever loading.
	navDeadlineCtx, cancelNav := context.WithTimeout(tabCtx, opt.NavTimeout)
	navErr := chromedp.Run(navDeadlineCtx, chromedp.ActionFunc(func(c context.Context) error {
		_, _, _, err := page.Navigate(targetURL).Do(c)
		return err
	}))
	navTimedOut := func() bool {
		return navDeadlineCtx.Err() == context.DeadlineExceeded
	}
	if navErr != nil {
		mu.Lock()
		pe := policyErr
		mu.Unlock()
		switch {
		case pe != nil:
			cancelNav()
			addEvent("policy_violation", pe.Error())
			return nil, &CollectError{Class: models.FailPolicy, Message: pe.Error()}
		case browserExited():
			cancelNav()
			return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited during navigation: " + navErr.Error()}
		case navTimedOut():
			cancelNav()
			return nil, &CollectError{Class: models.FailNavigationTimeout, Message: "navigation did not complete within " + opt.NavTimeout.String()}
		default:
			cancelNav()
			if strings.Contains(navErr.Error(), "ERR_BLOCKED") || strings.Contains(navErr.Error(), "ERR_TOO_MANY_REDIRECTS") {
				return nil, &CollectError{Class: models.FailPolicy, Message: "navigation blocked: " + navErr.Error()}
			}
			return nil, &CollectError{Class: models.FailCollector, Message: "navigate: " + navErr.Error()}
		}
	}

	// Wait for the load event under the same navigation deadline. Redirect
	// chains are denied asynchronously from Fetch callbacks, so a policy error
	// may surface either as a navigation error or as the failure settles.
	select {
	case <-loadCh:
		cancelNav()
		mu.Lock()
		pe := policyErr
		mu.Unlock()
		if pe != nil {
			return nil, &CollectError{Class: models.FailPolicy, Message: pe.Error()}
		}
	case <-navDeadlineCtx.Done():
		cancelNav()
		mu.Lock()
		pe := policyErr
		mu.Unlock()
		switch {
		case pe != nil:
			return nil, &CollectError{Class: models.FailPolicy, Message: pe.Error()}
		case browserExited():
			return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited before load event"}
		default:
			return nil, &CollectError{Class: models.FailNavigationTimeout,
				Message: "load event not fired within " + opt.NavTimeout.String()}
		}
	case <-exitedCh:
		cancelNav()
		return nil, &CollectError{Class: models.FailBrowserExited, Message: fmt.Sprintf("chromium exited before load event (wait: %v)", waitErr)}
	}

	// Optional browser crash used by the crash-recovery test.
	if opt.CrashAfterLoad {
		addEvent("test_hook", "triggering browser.crash")
		_ = chromedp.Run(browserCtx, browser.Crash())
		<-exitedCh
		return nil, &CollectError{Class: models.FailBrowserExited, Message: "browser crashed deliberately (test hook)"}
	}

	// Quiet observation window for late LCP / layout shifts / long tasks.
	select {
	case <-time.After(opt.SettleDelay):
	case <-exitedCh:
		return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited during settle window"}
	}

	// Extraction phase (short, separate deadline so it never masquerades as nav timeout).
	var raw string
	extractCtx, extractCancel := context.WithTimeout(tabCtx, 10*time.Second)
	err = chromedp.Run(extractCtx, chromedp.Evaluate(extractJS, &raw,
		func(p *runtime.EvaluateParams) *runtime.EvaluateParams { return p.WithReturnByValue(true) }))
	extractCancel()
	if err != nil {
		if browserExited() {
			return nil, &CollectError{Class: models.FailBrowserExited, Message: "chromium exited during metric extraction"}
		}
		return nil, &CollectError{Class: models.FailCollector, Message: "extract metrics: " + err.Error()}
	}
	var data extractedData
	if err := json.Unmarshal([]byte(raw), &data); err != nil {
		return nil, &CollectError{Class: models.FailCollector, Message: "parse metrics payload: " + err.Error()}
	}

	mu.Lock()
	hops := guard.Hops()
	resEvents := events
	var resResources []*models.ResourceEntry
	for _, row := range rows {
		if !row.gotStart && !row.blocked && !row.failed {
			continue
		}
		er := &models.ResourceEntry{
			URL:          trunc(row.url, 2000),
			ResourceType: row.rtype,
			Initiator:    row.initiator,
			HTTPStatus:   row.httpStatus,
			EncodedBytes: row.bytes,
		}
		if row.gotStart && navStartSet {
			s := monoMS(navStartMono, row.start)
			er.StartMS = &s
		}
		if row.gotFinish && navStartSet {
			e := monoMS(navStartMono, row.end)
			er.EndMS = &e
			if row.gotStart {
				d := monoMS(row.start, row.end)
				if d < 0 {
					d = 0
				}
				er.DurationMS = &d
			}
		}
		switch {
		case row.blocked:
			er.Status = "blocked"
			er.BlockedReason = "allow-list or non-http denial"
		case row.failed:
			er.Status = "failed"
			er.BlockedReason = trunc(row.failText, 240)
		default:
			er.Status = "loaded"
		}
		resResources = append(resResources, er)
	}
	rf, bc := resourceFails, blockedCount
	mu.Unlock()

	metrics := buildMetrics(&data)

	if len(data.Errors) > 0 {
		resEvents = append(resEvents, &models.RunEvent{
			Kind:    "metric_observer_errors",
			Message: trunc(strings.Join(data.Errors, "; "), 1000),
		})
	}
	if rf > 0 {
		resEvents = append(resEvents, &models.RunEvent{
			Kind:    "partial_resource_failure",
			Message: fmt.Sprintf("%d subresource(s) failed to load; run still reported with metrics", rf),
		})
	}
	if bc > 0 {
		resEvents = append(resEvents, &models.RunEvent{
			Kind:    "resources_blocked",
			Message: fmt.Sprintf("%d request(s) denied by the allow-list", bc),
		})
	}

	finalURL := data.Location
	if finalURL == "" {
		finalURL = targetURL
	}
	return &Result{
		FinalURL:         finalURL,
		RedirectHops:     hops,
		Metrics:          metrics,
		Resources:        resResources,
		Events:           resEvents,
		ResourceFailures: rf,
		BlockedResources: bc,
	}, nil
}

// handlePaused enforces the allow-list for every request, counts redirects and
// either continues or fails the paused request.
func handlePaused(
	actCtx context.Context,
	e *fetch.EventRequestPaused,
	checker *policy.Checker,
	guard *policy.RedirectGuard,
	mu *sync.Mutex,
	rows map[network.RequestID]*resRow,
	blockedIDs map[network.RequestID]bool,
	addEvent func(kind, msg string),
	setPolicyErr func(*policy.Violation),
	incBlocked func(),
) {
	isDocument := e.ResourceType == network.ResourceTypeDocument
	// Fetch interception ids live in their own namespace; NetworkID is the
	// matching Network domain request id and is the key into `rows`.
	rid := e.NetworkID
	if rid == "" {
		rid = network.RequestID(e.RequestID)
	}

	// RedirectedRequestID is set exactly on follow-up requests created because
	// the previous response was a redirect. In the Fetch domain it holds the
	// interception job id (not a Network.RequestID), so it is used purely as a
	// "this is a redirect hop" signal. The engine has not fetched the next URL
	// yet at this point; denying the follow-up aborts the whole chain.
	if e.RedirectedRequestID != "" {
		addEvent("redirect", fmt.Sprintf("hop %d: -> %s",
			guard.Hops()+1, e.Request.URL))
		if v := guard.Hop(e.Request.URL); v != nil {
			setPolicyErr(v)
			addEvent("policy_violation", v.Error())
			_ = fetch.FailRequest(e.RequestID, network.ErrorReasonFailed).Do(actCtx)
			return
		}
	}

	v := checker.Check(e.Request.URL)
	if v != nil {
		mu.Lock()
		blockedIDs[rid] = true
		r := rows[rid]
		if r == nil {
			// A request denied at the pause point may never produce a
			// requestWillBeSent row; record it so the waterfall still shows
			// the denial with its URL and resource type.
			r = &resRow{url: e.Request.URL, rtype: string(e.ResourceType)}
			rows[rid] = r
		}
		r.blocked = true
		r.failText = v.Code
		if r.rtype == "" {
			r.rtype = string(e.ResourceType)
		}
		mu.Unlock()
		addEvent("request_blocked", fmt.Sprintf("%s %s (%s)", e.ResourceType, e.Request.URL, v.Code))
		if isDocument {
			// Main-frame documents are only the allow-listed target or its
			// redirects; a violation here fails the whole run.
			setPolicyErr(v)
		} else {
			incBlocked()
		}
		_ = fetch.FailRequest(e.RequestID, network.ErrorReasonAccessDenied).Do(actCtx)
		return
	}
	_ = fetch.ContinueRequest(e.RequestID).Do(actCtx)
}

// waitForDevToolsPort polls Chromium's DevToolsActivePort file for the WS port.
func waitForDevToolsPort(portFile string, exitedCh <-chan struct{}, timeout time.Duration) (string, error) {
	deadline := time.Now().Add(timeout)
	client := &http.Client{Timeout: time.Second}
	for time.Now().Before(deadline) {
		select {
		case <-exitedCh:
			return "", errors.New("chromium exited before writing DevToolsActivePort")
		default:
		}
		b, err := os.ReadFile(portFile)
		if err == nil {
			lines := strings.Split(strings.TrimSpace(string(b)), "\n")
			if len(lines) >= 1 && lines[0] != "" {
				port := strings.TrimSpace(lines[0])
				if _, perr := strconv.Atoi(port); perr == nil {
					if _, herr := client.Get("http://127.0.0.1:" + port + "/json/version"); herr == nil {
						return port, nil
					}
				}
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	return "", errors.New("timed out waiting for Chromium DevTools port")
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
