package browser

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/emulation"
	"github.com/chromedp/cdproto/fetch"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"

	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/whitelist"
)

// Collector runs CDP collections against one browser Instance.
type Collector struct {
	instance *Instance
	strict   bool
}

func NewCollector(inst *Instance, strictSubresources bool) *Collector {
	return &Collector{instance: inst, strict: strictSubresources}
}

// Diagnostics are partial observations recorded even when a run fails,
// so timeouts and policy blocks keep their redirect/violation evidence.
type Diagnostics struct {
	Redirects  []models.Redirect
	Violations []models.Violation
	Warnings   []string
	Resources  []models.Resource
}

// CollectOptions configures one navigation/collection.
type CollectOptions struct {
	TargetURL  string
	Profile    whitelist.DeviceProfile
	Matcher    *whitelist.Matcher
	NavTimeout time.Duration
	Settle     time.Duration
	// MaxRedirects bounds the main-document redirect chain. The navigation
	// is blocked (POLICY_BLOCKED / REDIRECT_LIMIT) once exceeded, so a loop
	// of whitelisted redirects cannot pin a browser forever.
	MaxRedirects int
}

// DefaultMaxRedirects is applied when CollectOptions leaves it unset.
const DefaultMaxRedirects = 10

// Collect performs one navigation and returns measured metrics. Every return
// path cancels its tab context, which releases the tab/network resources.
func (c *Collector) Collect(parent context.Context, opts CollectOptions) (rm *store.RunMetrics, d *Diagnostics, retErr error) {
	// Pre-flight: the task URL itself must be http(s) and whitelisted.
	if _, err := opts.Matcher.ValidateTarget(opts.TargetURL); err != nil {
		return nil, nil, fail(CodePolicyBlocked, "%v", err)
	}

	tabCtx, tabCancel := chromedp.NewContext(c.instance.Context())
	defer tabCancel()

	state := newCollectState(opts.Matcher, c.strict, opts.MaxRedirects)
	chromedp.ListenTarget(tabCtx, state.handleEvent)

	// Crash watch is per-collection: browser process death mid-navigation must
	// be distinguishable from a slow page (timeout) or dead resource.
	crashCh := c.instance.Crashed()

	if err := setupTab(tabCtx, opts.Profile); err != nil {
		return nil, nil, fail(CodeInternal, "enable domains: %v", err)
	}

	// Fetch responses must be issued with the target-session executor, which
	// only exists after the first chromedp.Run has attached the target. Run a
	// single responder on an executor-bearing context; paused requests are
	// queued to it by the event handler (events can fire before this action).
	state.startResponder(tabCtx)

	// Start intercepting all request stages (covers redirect hops too:
	// every redirected request produces a new Fetch.requestPaused event).
	if err := chromedp.Run(tabCtx, chromedp.ActionFunc(func(ctx context.Context) error {
		return fetch.Enable().WithPatterns([]*fetch.RequestPattern{
			{RequestStage: fetch.RequestStageRequest},
		}).Do(ctx)
	})); err != nil {
		return nil, nil, fail(CodeInternal, "fetch enable: %v", err)
	}

	navTimeout := opts.NavTimeout
	if navTimeout <= 0 {
		navTimeout = 30 * time.Second
	}
	// Cancel the navigation on either the timeout or the parent (task/lease
	// cancellation), so a reclaimed lease abandons the browser work promptly.
	navCtx, navCancel := context.WithTimeout(tabCtx, navTimeout)
	defer navCancel()
	stopWatch := context.AfterFunc(parent, func() { navCancel() })
	defer stopWatch()

	navErrCh := make(chan error, 1)
	go func() {
		err := chromedp.Run(navCtx, chromedp.ActionFunc(func(ctx context.Context) error {
			_, _, errorText, e := page.Navigate(opts.TargetURL).Do(ctx)
			if e == nil && errorText != "" {
				e = fmt.Errorf("%s", errorText)
			}
			return e
		}))
		navErrCh <- err
	}()

	var navErr error
	select {
	case <-state.loadCh:
		// load event fired; the navigation action itself should be done.
		select {
		case navErr = <-navErrCh:
		default:
			navErr = <-navErrCh
		}
	case info := <-state.docBlockCh:
		// The document was blocked/failed inside the browser. The Navigate CDP
		// action can nonetheless stay pending until its context ends, so cancel
		// it explicitly and drain the goroutine without blocking the verdict.
		navCancel()
		select {
		case <-navErrCh:
		case <-time.After(2 * time.Second):
		}
		if browserIsDead(crashCh) {
			return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during navigation")
		}
		return nil, state.diagnostics(), fail(info.code, "%s", info.msg)
	case <-navCtx.Done():
		<-navErrCh
		// Killing the browser cancels the tab context, which cancels navCtx
		// too. If the deadline did not actually elapse, this is a connection
		// death: give the process-exit signal a short grace window, then
		// classify from the authoritative crash channel.
		if navCtx.Err() != context.DeadlineExceeded {
			select {
			case <-crashCh:
				return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during navigation")
			case <-time.After(2 * time.Second):
			}
		}
		if browserIsDead(crashCh) {
			return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during navigation")
		}
		d := state.diagnostics()
		d.Warnings = append(d.Warnings, fmt.Sprintf("load event did not fire within %s; navigation aborted", navTimeout))
		return nil, d, fail(CodeNavigationTimeout, "load event did not fire within %s", navTimeout)
	case <-crashCh:
		<-navErrCh
		return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during navigation")
	case <-parent.Done():
		<-navErrCh
		return nil, state.diagnostics(), fail(CodeTaskCancelled, "task cancelled: %v", parent.Err())
	}
	if navErr != nil {
		if browserIsDead(crashCh) {
			return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during navigation: %v", navErr)
		}
		if state.docBlocked.Load() {
			return nil, state.diagnostics(), fail(CodePolicyBlocked, "main document blocked by policy: %v", navErr)
		}
		return nil, state.diagnostics(), fail(CodeNavigationFailed, "navigation error: %v", navErr)
	}

	// Settle window: give late LCP candidates, layout shifts and long tasks
	// time to happen and be observed before harvest.
	settle := opts.Settle
	if settle <= 0 {
		settle = 3 * time.Second
	}
	select {
	case <-time.After(settle):
	case <-crashCh:
		return nil, state.diagnostics(), fail(CodeBrowserCrash, "chromium process exited during settle")
	case <-parent.Done():
		return nil, state.diagnostics(), fail(CodeTaskCancelled, "task cancelled: %v", parent.Err())
	}

	metrics, err := harvest(tabCtx)
	if err != nil {
		return nil, state.diagnostics(), fail(CodeMetricHarvest, "%v", err)
	}

	// Stop interception so the tab tears down cleanly.
	_ = chromedp.Run(tabCtx, chromedp.ActionFunc(func(ctx context.Context) error {
		return fetch.Disable().Do(ctx)
	}))

	d = state.diagnostics()
	resources := state.assembleResources(metrics.jsResources)
	metrics.NavBaseMono = state.navBaseMono
	rm = fillRunMetrics(metrics, resources, d)

	finalURL := state.finalDocURL
	if finalURL == "" {
		finalURL = opts.TargetURL
	}
	rm.FinalURL = finalURL
	rm.Redirects = d.Redirects
	rm.Violations = d.Violations
	rm.Warnings = d.Warnings
	rm.Resources = resources
	rm.WindowEnd = time.Now().UTC()
	return rm, d, nil
}

// browserIsDead is a non-blocking check of the process-exit channel.
func browserIsDead(ch <-chan struct{}) bool {
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// setupTab enables the domains we consume, installs the metrics observer
// before page scripts exist, and applies device emulation.
func setupTab(ctx context.Context, p whitelist.DeviceProfile) error {
	return chromedp.Run(ctx,
		network.Enable(),
		page.Enable(),
		chromedp.ActionFunc(func(ctx context.Context) error {
			_, err := page.AddScriptToEvaluateOnNewDocument(metricsObserverScript).Do(ctx)
			return err
		}),
		emulation.SetDeviceMetricsOverride(p.Width, p.Height, p.Scale, p.Mobile).
			WithScreenWidth(p.Width).WithScreenHeight(p.Height).
			WithPositionX(0).WithPositionY(0),
		emulation.SetUserAgentOverride(p.UA),
	)
}

// collectState accumulates Network/Fetch/Page events for one collection.
type collectState struct {
	mu      sync.Mutex
	matcher *whitelist.Matcher
	strict  bool

	resByID map[network.RequestID]*trackedResource
	order   []network.RequestID
	// blocked maps Network-domain request IDs -> blocked. Populated when the
	// paused Fetch request carries a networkId (the normal case).
	blocked map[network.RequestID]bool

	docLoaderID cdp.LoaderID
	docChain    []models.Redirect
	finalDocURL string
	navBaseMono float64
	navBaseSet  bool
	docHops     int // number of paused document requests (initial + each redirect)
	maxRedirect int

	violations []models.Violation
	loadCh     chan struct{}
	loadOnce   sync.Once
	docBlockCh chan docBlock
	docBlocked atomicBool

	// respondCh serializes Fetch.ContinueRequest/FailRequest replies so they
	// are always issued with the attached target executor.
	respondCh   chan fetchDecision
	respondOnce sync.Once
}

type fetchDecision struct {
	id   fetch.RequestID
	fail bool
}

type docBlock struct{ code, msg string }

// startResponder consumes paused-request decisions until the tab context
// ends. It must be started with a context derived from the tab before the
// first chromedp.Run that attaches the target completes; events queued before
// attachment are buffered on respondCh.
func (s *collectState) startResponder(tabCtx context.Context) {
	s.respondOnce.Do(func() {
		go func() {
			for {
				select {
				case <-tabCtx.Done():
					return
				case d := <-s.respondCh:
					// The tab is torn down on timeout/cancel, which aborts the
					// reply; a short local timeout keeps shutdown prompt.
					replyCtx, cancel := context.WithTimeout(tabCtx, 10*time.Second)
					_ = chromedp.Run(replyCtx, chromedp.ActionFunc(func(actCtx context.Context) error {
						if d.fail {
							return fetch.FailRequest(d.id, network.ErrorReasonBlockedByClient).Do(actCtx)
						}
						return fetch.ContinueRequest(d.id).Do(actCtx)
					}))
					cancel()
				}
			}
		}()
	})
}

func newCollectState(m *whitelist.Matcher, strict bool, maxRedirects int) *collectState {
	if maxRedirects <= 0 {
		maxRedirects = DefaultMaxRedirects
	}
	return &collectState{
		matcher:     m,
		strict:      strict,
		maxRedirect: maxRedirects,
		resByID:     map[network.RequestID]*trackedResource{},
		blocked:     map[network.RequestID]bool{},
		loadCh:      make(chan struct{}),
		docBlockCh:  make(chan docBlock, 4),
		respondCh:   make(chan fetchDecision, 4096),
	}
}

type trackedResource struct {
	url         string
	method      string
	rtype       string
	isDocument  bool
	loaderID    cdp.LoaderID
	startMono   float64 // network event timestamp (monotonic seconds)
	finishMono  float64
	status      int64
	mime        string
	timing      *network.ResourceTiming
	finished    bool
	failed      bool
	canceled    bool
	failureText string
}

// mono dereferences a CDP monotonic timestamp to fractional seconds in the
// same time domain as ResourceTiming.RequestTime.
func mono(t *cdp.MonotonicTime) float64 {
	if t == nil {
		return 0
	}
	return float64(t.Time().Sub(*cdp.MonotonicTimeEpoch)) / float64(time.Second)
}

func (s *collectState) handleEvent(ev interface{}) {
	switch e := ev.(type) {
	case *fetch.EventRequestPaused:
		s.onPaused(e)
	case *network.EventRequestWillBeSent:
		s.onWillBeSent(e)
	case *network.EventResponseReceived:
		s.onResponse(e)
	case *network.EventLoadingFinished:
		s.onFinished(e)
	case *network.EventLoadingFailed:
		s.onLoadingFailed(e)
	case *page.EventLoadEventFired:
		s.loadOnce.Do(func() { close(s.loadCh) })
	}
}

// onPaused is the policy gate: scheme + whitelist checks for documents on
// every hop, and subresource checks (record always, block in strict mode).
func (s *collectState) onPaused(e *fetch.EventRequestPaused) {
	isDoc := e.ResourceType == network.ResourceTypeDocument
	stage := "subresource"
	if isDoc {
		stage = "document"
	}
	raw := e.Request.URL
	if isDoc {
		s.mu.Lock()
		s.docHops++
		hops := s.docHops
		overLimit := hops > s.maxRedirect+1
		s.mu.Unlock()
		if overLimit {
			s.mu.Lock()
			s.violations = append(s.violations, models.Violation{URL: raw, Kind: "redirect_limit", Stage: "redirect", Blocked: true})
			s.docBlocked.set(true)
			s.markBlocked(e.NetworkID)
			s.mu.Unlock()
			s.queueRespond(e.RequestID, true)
			s.docBlockCh <- docBlock{CodeRedirectLimit, fmt.Sprintf("redirect chain exceeded limit of %d hops", s.maxRedirect)}
			return
		}
	}
	parsed, perr := whitelist.ParseHTTPURL(raw)
	block := false
	kind := ""
	switch {
	case perr != nil:
		block = true
		kind = "scheme"
		if isDoc {
			s.mu.Lock()
			s.violations = append(s.violations, models.Violation{URL: raw, Kind: "scheme", Stage: "redirect", Blocked: true})
			s.docBlocked.set(true)
			s.markBlocked(e.NetworkID)
			s.mu.Unlock()
			s.queueRespond(e.RequestID, true)
			s.docBlockCh <- docBlock{CodePolicyBlocked, fmt.Sprintf("non-HTTP protocol in document/redirect chain: %s", raw)}
			return
		}
	case isDoc && !s.matcher.Allows(parsed):
		block = true
		kind = "not_whitelisted"
		s.mu.Lock()
		s.violations = append(s.violations, models.Violation{URL: raw, Kind: "not_whitelisted", Stage: "redirect", Blocked: true})
		s.docBlocked.set(true)
		s.markBlocked(e.NetworkID)
		s.mu.Unlock()
		s.queueRespond(e.RequestID, true)
		s.docBlockCh <- docBlock{CodePolicyBlocked, fmt.Sprintf("redirect target %s is outside the whitelist", raw)}
		return
	case !isDoc && !s.matcher.Allows(parsed):
		kind = "not_whitelisted"
		block = s.strict // non-strict: allow the load, record the violation
	}

	s.mu.Lock()
	if kind != "" {
		s.violations = append(s.violations, models.Violation{URL: raw, Kind: kind, Stage: stage, Blocked: block})
	}
	if block {
		s.markBlocked(e.NetworkID)
	}
	s.mu.Unlock()

	// Every paused request must receive exactly one reply, otherwise the
	// browser loader stalls and one bad request would hang the whole browser.
	s.queueRespond(e.RequestID, block)
}

// markBlocked records the blocked flag against the Network-domain request id
// when Fetch supplies a networkId; caller must hold s.mu.
func (s *collectState) markBlocked(nid network.RequestID) {
	if nid != "" {
		s.blocked[nid] = true
	}
}

// queueRespond enqueues a Fetch reply. The channel is generously buffered;
// the single responder goroutine (started before Fetch.enable) owns all
// replies so they always carry the attached target executor.
func (s *collectState) queueRespond(id fetch.RequestID, failReq bool) {
	s.respondCh <- fetchDecision{id: id, fail: failReq}
}

func (s *collectState) onWillBeSent(e *network.EventRequestWillBeSent) {
	s.mu.Lock()
	defer s.mu.Unlock()
	isDoc := e.Type == network.ResourceTypeDocument
	if isDoc {
		if !s.navBaseSet {
			s.navBaseSet = true
			s.navBaseMono = mono(e.Timestamp)
			s.docLoaderID = e.LoaderID
		}
		// A redirect: the previous document response is carried inline.
		if e.RedirectResponse != nil {
			allowed := hopAllowed(s.matcher, e.RedirectResponse.URL)
			s.docChain = append(s.docChain, models.Redirect{
				URL:         e.RedirectResponse.URL,
				Status:      int64(e.RedirectResponse.Status),
				Whitelisted: allowed,
				Allowed:     allowed,
			})
		}
	}
	r := &trackedResource{
		url:        e.Request.URL,
		method:     e.Request.Method,
		rtype:      string(e.Type),
		isDocument: isDoc,
		loaderID:   e.LoaderID,
		startMono:  mono(e.Timestamp),
	}
	s.resByID[e.RequestID] = r
	s.order = append(s.order, e.RequestID)
}

func (s *collectState) onResponse(e *network.EventResponseReceived) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.resByID[e.RequestID]
	if !ok {
		return
	}
	r.status = int64(e.Response.Status)
	r.mime = e.Response.MimeType
	r.timing = e.Response.Timing
	if r.isDocument && r.status != 0 {
		s.finalDocURL = e.Response.URL
	}
}

func (s *collectState) onFinished(e *network.EventLoadingFinished) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r, ok := s.resByID[e.RequestID]; ok {
		r.finished = true
		r.finishMono = mono(e.Timestamp)
		if r.isDocument {
			s.finalDocURL = r.url
			s.docChain = appendDocFinal(s.docChain, r)
		}
	}
}

func (s *collectState) onLoadingFailed(e *network.EventLoadingFailed) {
	s.mu.Lock()
	defer s.mu.Unlock()
	blockedByPolicy := s.blocked[e.RequestID]
	r, ok := s.resByID[e.RequestID]
	if !ok {
		r = &trackedResource{rtype: string(e.Type)}
		s.resByID[e.RequestID] = r
		s.order = append(s.order, e.RequestID)
	}
	if r.isDocument {
		switch {
		case blockedByPolicy:
			s.docBlocked.set(true)
			s.docBlockCh <- docBlock{CodePolicyBlocked, fmt.Sprintf("main document blocked: %s", e.ErrorText)}
		case isUnsafeRedirect(e.ErrorText):
			// Browser refused the hop (usually a non-HTTP scheme target).
			// Policy-class outcome, distinct from a plain network failure.
			// s.mu is already held by this method.
			s.violations = append(s.violations, models.Violation{
				URL: r.url, Kind: "scheme", Stage: "redirect", Blocked: true,
			})
			s.docBlocked.set(true)
			s.docBlockCh <- docBlock{CodePolicyBlocked, fmt.Sprintf("browser refused unsafe redirect: %s", e.ErrorText)}
		default:
			s.docBlockCh <- docBlock{CodeNavigationFailed, fmt.Sprintf("main document failed: %s", e.ErrorText)}
		}
		return
	}
	// Canceled is normal (page teardown, XHR abort). A blocked-by-policy
	// request is a policy outcome, not a network failure.
	if !blockedByPolicy && e.ErrorText != "net::ERR_ABORTED" {
		r.failed = true
		r.failureText = e.ErrorText
	} else {
		r.canceled = true
		r.failureText = e.ErrorText
	}
}

func (s *collectState) diagnostics() *Diagnostics {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := &Diagnostics{
		Redirects:  append([]models.Redirect(nil), s.docChain...),
		Violations: append([]models.Violation(nil), s.violations...),
	}
	if n := failedSubresources(s.resByID, s.order, s.blocked); n > 0 {
		d.Warnings = append(d.Warnings, fmt.Sprintf("%d subresource(s) failed to load (individual failures do not fail the task)", n))
	}
	if n := httpErrorSubresources(s.resByID, s.order, s.blocked); n > 0 {
		d.Warnings = append(d.Warnings, fmt.Sprintf("%d subresource(s) returned HTTP 4xx/5xx (individual errors do not fail the task)", n))
	}
	if n := len(s.violations); n > 0 {
		d.Warnings = append(d.Warnings, fmt.Sprintf("%d whitelist/protocol violation(s) recorded", n))
	}
	d.Resources = s.assembleResourcesLocked(nil)
	return d
}

// assembleResources converts tracked network events into the waterfall,
// enriching cache flags from JS resource timing entries.
func (s *collectState) assembleResources(js []jsResource) []models.Resource {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.assembleResourcesLocked(js)
}

func (s *collectState) assembleResourcesLocked(js []jsResource) []models.Resource {
	jsCache := map[string]bool{}
	for _, j := range js {
		if j.TransferSize == 0 && j.DecodedBodySize > 0 {
			jsCache[j.Name] = true
		}
	}
	out := make([]models.Resource, 0, len(s.order))
	for _, id := range s.order {
		r := s.resByID[id]
		res := models.Resource{
			URL: r.url, Type: r.rtype, Method: r.method,
			Status: r.status, MimeType: r.mime,
			Failed: r.failed, Blocked: s.blocked[id], FailureText: r.failureText,
			FromCache: jsCache[r.url],
		}
		applyTiming(&res, r, s.navBaseMono)
		out = append(out, res)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].StartMS < out[j].StartMS })
	return out
}

func failedSubresources(m map[network.RequestID]*trackedResource, order []network.RequestID, blocked map[network.RequestID]bool) int {
	n := 0
	for _, id := range order {
		r := m[id]
		if r != nil && !r.isDocument && r.failed && !blocked[id] {
			n++
		}
	}
	return n
}

// httpErrorSubresources counts completed subresource responses with 4xx/5xx
// status: they load successfully at the network layer but still failed from
// the page's perspective.
func httpErrorSubresources(m map[network.RequestID]*trackedResource, order []network.RequestID, blocked map[network.RequestID]bool) int {
	n := 0
	for _, id := range order {
		r := m[id]
		if r == nil || r.isDocument || blocked[id] || r.failed {
			continue
		}
		if r.status >= 400 {
			n++
		}
	}
	return n
}

func hopAllowed(m *whitelist.Matcher, raw string) bool {
	u, err := whitelist.ParseHTTPURL(raw)
	if err != nil {
		return false
	}
	return m.Allows(u)
}

// isUnsafeRedirect reports whether the browser refused a navigation/redirect
// for security reasons (non-HTTP scheme target, dangerous protocol, …). These
// are policy outcomes rather than ordinary network failures.
func isUnsafeRedirect(errorText string) bool {
	return strings.Contains(errorText, "ERR_UNSAFE_REDIRECT") ||
		strings.Contains(errorText, "ERR_BLOCKED_BY") ||
		strings.Contains(errorText, "ERR_DISALLOWED")
}

func appendDocFinal(chain []models.Redirect, r *trackedResource) []models.Redirect {
	// Avoid duplicating the final hop when loadingFinished fires after a
	// responseReceived already recorded it.
	if len(chain) > 0 && chain[len(chain)-1].URL == r.url && chain[len(chain)-1].Status == r.status {
		return chain
	}
	return append(chain, models.Redirect{URL: r.url, Status: r.status, Whitelisted: true, Allowed: true})
}

// applyTiming converts CDP monotonic timings to offsets from the document's
// navigation base in milliseconds. Cross-origin responses without
// Timing-Allow-Origin expose -1 phase starts: mark them restricted.
func applyTiming(res *models.Resource, r *trackedResource, navBase float64) {
	ms := func(v float64) float64 { return v * 1000 }
	// Start offset: ResourceTiming requestTime when available, else the
	// willBeSent monotonic timestamp.
	if r.timing != nil && r.timing.RequestTime > 0 {
		v := ms(r.timing.RequestTime - navBase)
		if v > 0 {
			res.StartMS = v
		}
	} else if r.startMono > 0 && navBase > 0 {
		v := ms(r.startMono - navBase)
		if v > 0 {
			res.StartMS = v
		}
	}
	if r.timing != nil && r.timing.RequestTime > 0 {
		t := r.timing
		restricted := t.DNSStart < 0 && t.ConnectStart < 0 && t.SendStart < 0
		if !restricted {
			if t.DNSEnd >= t.DNSStart && t.DNSStart >= 0 {
				v := ms(t.DNSEnd - t.DNSStart)
				res.DNSMS = &v
			}
			if t.ConnectEnd >= t.ConnectStart && t.ConnectStart >= 0 {
				v := ms(t.ConnectEnd - t.ConnectStart)
				res.ConnectMS = &v
			}
			if t.SslStart >= 0 && t.ConnectEnd > t.SslStart {
				v := ms(t.ConnectEnd - t.SslStart)
				res.SSLMS = &v
			}
			if t.ReceiveHeadersEnd > t.SendEnd && t.SendEnd >= 0 {
				v := ms(t.ReceiveHeadersEnd - t.SendEnd)
				res.RequestMS = &v
			}
			if t.ReceiveHeadersEnd > t.SendStart && t.SendStart >= 0 {
				// Headers received through body download: exact body phase is
				// unavailable in this CDP revision; duration comes from events.
				_ = t.ReceiveHeadersEnd
			}
		} else {
			// Cross-origin without Timing-Allow-Origin: phases are hidden.
			res.TimingRestricted = true
		}
	}
	// End-to-end duration from CDP event deltas when phase timing is hidden
	// or missing. Both events really occurred; this is not fabricated.
	if res.DurationMS == 0 && r.finishMono > r.startMono {
		res.DurationMS = ms(r.finishMono - r.startMono)
	}
}
