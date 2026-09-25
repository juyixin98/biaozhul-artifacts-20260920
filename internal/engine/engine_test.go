package engine_test

import (
	"testing"
	"time"

	"github.com/example/hysteresis-alerter/internal/engine"
)

// testClock is a fixed origin for engine-driven tests.
var tc = engine.Epoch.Add(24 * time.Hour)

func d(s string) time.Duration {
	dur, err := time.ParseDuration(s)
	return must(dur, err)
}

func must[T any](v T, err error) T {
	if err != nil {
		panic(err)
	}
	return v
}

func newTestEngine() *engine.Engine { return engine.NewEngine() }

func baseRule(id, metric string) engine.Rule {
	return engine.Rule{
		ID:         id,
		Metric:     metric,
		Operator:   engine.OpGreaterOrEqual,
		Threshold:  80,
		TriggerFor: engine.Duration{Duration: d("60s")},
		RecoverFor: engine.Duration{Duration: d("90s")},
		NoDataFor:  engine.Duration{Duration: d("2m")},
	}
}

func send(e *engine.Engine, t time.Time, metric string, v float64) []engine.Event {
	res := e.Ingest([]engine.IngestItem{{Metric: metric, TS: t, Value: v}})
	var evs []engine.Event
	for _, r := range res {
		evs = append(evs, r.Events...)
	}
	return evs
}

func tick(e *engine.Engine, dur time.Duration) []engine.Event {
	return must[[]engine.Event](e.Tick(dur))
}

func stateOf(e *engine.Engine, id string) engine.State {
	rs, ok := e.GetRule(id)
	if !ok {
		return "<missing>"
	}
	return rs.State.State
}

func typesOf(evs []engine.Event) []engine.EventType {
	out := make([]engine.EventType, 0, len(evs))
	for _, ev := range evs {
		out = append(out, ev.Type)
	}
	return out
}

// TestThresholdJitter verifies the headline acceptance scenario: samples
// oscillating around the threshold must not fire while the hot streak stays
// shorter than trigger_for; warm values must hold an open alert open; only a
// sustained cold streak resolves.
func TestThresholdJitter(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "cpu")
	// Recovery band: 70..80 is warm.
	r.RecoveryThreshold = 70
	r.HasRecovery = true
	if err := e.CreateRule(r); err != nil {
		t.Fatalf("create: %v", err)
	}

	// Oscillate hot/warm every 30s. Each hot streak is at most 30s long,
	// below trigger_for=60s: never pending->firing.
	for i := 0; i < 4; i++ {
		t0 := tc.Add(time.Duration(i) * 60 * time.Second)
		if evs := send(e, t0, "cpu", 90); len(evs) != 0 {
			t.Fatalf("hot sample %d emitted unexpected events: %v", i, typesOf(evs))
		}
		if got := stateOf(e, "r1"); got != engine.StatePending {
			t.Fatalf("after hot sample %d: state=%s want pending", i, got)
		}
		if evs := send(e, t0.Add(30*time.Second), "cpu", 75); len(evs) != 0 {
			t.Fatalf("warm sample %d emitted events: %v", i, typesOf(evs))
		}
		if got := stateOf(e, "r1"); got != engine.StateInactive {
			t.Fatalf("after warm sample %d: state=%s want inactive", i, got)
		}
	}

	// Sustained hot: 90 at t, +30, +60 -> fires exactly once at +60.
	t0 := tc.Add(4 * time.Minute)
	send(e, t0, "cpu", 90)
	send(e, t0.Add(30*time.Second), "cpu", 92)
	evs := send(e, t0.Add(60*time.Second), "cpu", 95)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventFiring {
		t.Fatalf("sustained hot: events=%v want [firing]", got)
	}
	if got := stateOf(e, "r1"); got != engine.StateFiring {
		t.Fatalf("state=%s want firing", got)
	}

	// One warm value must NOT start recovery — alert stays firing.
	if evs := send(e, t0.Add(90*time.Second), "cpu", 75); len(evs) != 0 {
		t.Fatalf("warm while firing emitted events: %v", typesOf(evs))
	}
	if got := stateOf(e, "r1"); got != engine.StateFiring {
		t.Fatalf("warm sample moved state to %s, want firing (hysteresis band)", got)
	}

	// One cold value starts recovering but does not resolve (90s needed).
	if evs := send(e, t0.Add(120*time.Second), "cpu", 50); len(evs) != 0 {
		t.Fatalf("first cold sample emitted notification: %v", typesOf(evs))
	}
	if got := stateOf(e, "r1"); got != engine.StateRecovering {
		t.Fatalf("state=%s want recovering", got)
	}
	// Warm again during recovery suspends the countdown (ColdSince reset)
	// but the value is not hot: state stays recovering, does NOT jump to
	// firing. Only a genuinely hot value re-fires without notification.
	send(e, t0.Add(150*time.Second), "cpu", 76)
	if got := stateOf(e, "r1"); got != engine.StateRecovering {
		t.Fatalf("warm during recovery: state=%s want recovering (countdown suspended)", got)
	}
	// A subsequent cold sample starts a FRESH 90s countdown; the interrupted
	// one must not count.
	send(e, t0.Add(160*time.Second), "cpu", 40)
	if evs := send(e, t0.Add(160*time.Second).Add(60*time.Second), "cpu", 41); len(evs) != 0 {
		t.Fatalf("resolved too early after warm interrupted recovery: %v", typesOf(evs))
	}
	evs = send(e, t0.Add(160*time.Second).Add(90*time.Second), "cpu", 42)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventResolved {
		t.Fatalf("sustained cold after band interruption: events=%v want [resolved]", got)
	}
	if got := stateOf(e, "r1"); got != engine.StateInactive {
		t.Fatalf("state=%s want inactive", got)
	}

	// No extra notifications anywhere in the event log beyond the pair.
	all := e.QueryEvents("", false, nil)
	var notifCount int
	for _, ev := range all {
		if ev.Type.IsNotification() {
			notifCount++
		}
	}
	if notifCount != 2 {
		t.Fatalf("total notifications=%d want 2 (firing, resolved)", notifCount)
	}
}

// TestLongNoData verifies staleness detection through ticking, repeated
// ticks while already in nodata not re-notifying, and data resumption.
func TestLongNoData(t *testing.T) {
	e := newTestEngine()
	if err := e.CreateRule(baseRule("r1", "mem")); err != nil {
		t.Fatal(err)
	}

	send(e, tc, "mem", 10)
	send(e, tc.Add(30*time.Second), "mem", 11)

	// 1m59s elapsed < no_data_for=2m: no event.
	if evs := tick(e, 119*time.Second); len(evs) != 0 {
		t.Fatalf("within no-data window emitted: %v", typesOf(evs))
	}
	// Cross the boundary at 2m.
	evs := tick(e, 1*time.Second)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventNodata {
		t.Fatalf("nodata transition: %v want [nodata]", got)
	}
	if got := stateOf(e, "r1"); got != engine.StateNodata {
		t.Fatalf("state=%s want nodata", got)
	}
	// Further long silence must not produce duplicate notifications.
	if evs := tick(e, time.Hour); len(evs) != 0 {
		t.Fatalf("continued silence re-notified: %v", typesOf(evs))
	}

	// Healthy data resumes -> data_resumed only, no firing.
	evs = send(e, tc.Add(2*time.Hour), "mem", 30)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventDataResumed {
		t.Fatalf("resume healthy: %v want [data_resumed]", got)
	}
	if got := stateOf(e, "r1"); got != engine.StateInactive {
		t.Fatalf("state=%s want inactive", got)
	}
}

// TestNoDataFiresBeforeAnySample ensures a rule with no samples at all also
// enters nodata after no_data_for from rule creation.
func TestNoDataBeforeAnySample(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "x")
	r.NoDataFor = engine.Duration{Duration: 30 * time.Second}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	if evs := tick(e, 29*time.Second); len(evs) != 0 {
		t.Fatalf("early nodata: %v", typesOf(evs))
	}
	if evs := tick(e, time.Second); len(evs) != 1 || evs[0].Type != engine.EventNodata {
		t.Fatalf("want one nodata event, got %v", typesOf(evs))
	}
}

// TestDuplicateSamplesDoNotAccumulate is the core duration rule: samples at
// the SAME virtual timestamp never extend streak durations. The clock moves
// only with distinct timestamps (or /clock/tick).
func TestDuplicateSamplesDoNotAccumulate(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "cpu")
	r.TriggerFor = engine.Duration{Duration: 60 * time.Second}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}

	// One hot sample then many duplicates at the same timestamp: the hot
	// streak stays at 0 elapsed, so no firing.
	send(e, tc, "cpu", 99)
	for i := 0; i < 10; i++ {
		res := e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: tc, Value: 99}})
		if !res[0].Duplicate {
			t.Fatal("repeat sample not marked duplicate")
		}
		if len(res[0].Events) != 0 {
			t.Fatalf("repeat sample %d emitted events", i)
		}
	}
	if got := stateOf(e, "r1"); got != engine.StatePending {
		t.Fatalf("state=%s want pending", got)
	}

	// Advancing the clock without new samples does not fire either; only
	// real elapsed virtual time combined with a hot latest value does.
	// Tick 59s: latest sample still hot, 59 < 60 -> remains pending.
	tick(e, 59*time.Second)
	if got := stateOf(e, "r1"); got != engine.StatePending {
		t.Fatalf("state=%s want pending at 59s", got)
	}
	// One more second, no new sample: the hot streak hits 60s and fires.
	evs := tick(e, time.Second)
	if len(evs) != 1 || evs[0].Type != engine.EventFiring {
		t.Fatalf("want firing at 60s elapsed virtual time, got %v", typesOf(evs))
	}

	// Overwrite the ORIGINAL sample (at tc) with a cold value. It is
	// duplicate+overwrote but since tc is already in the past it is treated
	// as late (the clock never retreats): it changes the stored value for
	// querying but cannot re-drive the state machine.
	res := e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: tc, Value: 10}})
	if !res[0].Duplicate || !res[0].Overwrote || !res[0].Late {
		t.Fatalf("same-ts overwrite in the past must be duplicate+overwrote+late, got dup=%v ov=%v late=%v",
			res[0].Duplicate, res[0].Overwrote, res[0].Late)
	}
	if got := stateOf(e, "r1"); got != engine.StateFiring {
		t.Fatalf("state=%s must remain firing (late overwrite does not re-evaluate)", got)
	}

	// A repeat at the current clock instant with a different value is a
	// genuine overwrite that re-evaluates at zero elapsed time. Firing with
	// a cold value enters recovering, but the 90s recovery countdown starts
	// from zero.
	now := e.Now()
	res = e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: now, Value: 10}})
	if res[0].Duplicate || res[0].Late {
		t.Fatalf("new timestamp must not be duplicate/late, got dup=%v late=%v", res[0].Duplicate, res[0].Late)
	}
	if got := stateOf(e, "r1"); got != engine.StateRecovering {
		t.Fatalf("state=%s want recovering after cold sample", got)
	}
	// Repeating the identical sample at that same timestamp is duplicate
	// without overwrite and must not advance the recovery countdown.
	res2 := e.Ingest([]engine.IngestItem{{Metric: "cpu", TS: now, Value: 10}})
	if !res2[0].Duplicate || res2[0].Overwrote {
		t.Fatal("identical repeat must be duplicate, not overwrote")
	}
	if evs := tick(e, 89*time.Second); len(evs) != 0 {
		t.Fatalf("recovery must not complete before recover_for: %v", typesOf(evs))
	}
	evs = tick(e, time.Second)
	if len(evs) != 1 || evs[0].Type != engine.EventResolved {
		t.Fatalf("want resolved at recover_for, got %v", typesOf(evs))
	}
}

// TestOutOfOrderSamples: late samples are stored and queryable but never
// drive the state machine nor emit events; the virtual clock never retreats.
func TestOutOfOrderSamples(t *testing.T) {
	e := newTestEngine()
	if err := e.CreateRule(baseRule("r1", "qps")); err != nil {
		t.Fatal(err)
	}

	send(e, tc.Add(2*time.Minute), "qps", 10)
	clockBefore := e.Now()

	// A late hot sample would fire if it were evaluated. It must not.
	res := e.Ingest([]engine.IngestItem{
		{Metric: "qps", TS: tc.Add(30 * time.Second), Value: 999},
		{Metric: "qps", TS: tc.Add(10 * time.Second), Value: 998},
	})
	for _, r := range res {
		if !r.Late {
			t.Fatalf("sample at %s must be marked late", r.TS)
		}
		if len(r.Events) != 0 {
			t.Fatalf("late sample emitted events: %v", typesOf(r.Events))
		}
	}
	if !e.Now().Equal(clockBefore) {
		t.Fatalf("clock moved after late samples: %s -> %s", clockBefore, e.Now())
	}
	if got := stateOf(e, "r1"); got != engine.StateInactive {
		t.Fatalf("state=%s want inactive after late samples", got)
	}

	// Late samples are still stored and queryable (backfill auditability).
	got := e.QuerySamples("qps", nil, nil, 0)
	if len(got) != 3 {
		t.Fatalf("stored samples=%d want 3 (late ones retained)", len(got))
	}

	// An unordered future batch is applied in timestamp order: only a
	// sustained streak may fire, and the clock ends at the max timestamp.
	res = e.Ingest([]engine.IngestItem{
		{Metric: "qps", TS: tc.Add(180 * time.Second), Value: 90}, // t+3m hot
		{Metric: "qps", TS: tc.Add(125 * time.Second), Value: 90}, // t+2m05 hot
	})
	var evs []engine.Event
	for _, r := range res {
		if r.Late {
			t.Fatal("future sample marked late")
		}
		evs = append(evs, r.Events...)
	}
	if !e.Now().Equal(tc.Add(180 * time.Second)) {
		t.Fatalf("clock=%s want t+180s", e.Now())
	}
	if got := stateOf(e, "r1"); got != engine.StatePending {
		t.Fatalf("state=%s want pending (sorted batch, 55s < 60s)", got)
	}
	if len(evs) != 0 {
		t.Fatalf("sorted future batch unexpectedly fired: %v", typesOf(evs))
	}
}

// TestNotificationsOnlyOnTransitions asserts that staying in firing with
// repeated hot samples never produces a second firing notification.
func TestNotificationsOnlyOnTransitions(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "cpu")
	r.RecoverFor = engine.Duration{Duration: 30 * time.Second}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	// Fire.
	send(e, tc, "cpu", 90)
	send(e, tc.Add(60*time.Second), "cpu", 91)
	if stateOf(e, "r1") != engine.StateFiring {
		t.Fatal("expected firing")
	}
	// Keep it hot across many distinct timestamps: no events.
	for i := 2; i <= 10; i++ {
		evs := send(e, tc.Add(time.Duration(i)*30*time.Second), "cpu", 90+float64(i))
		if len(evs) != 0 {
			t.Fatalf("hot sample %d emitted %v while still firing", i, typesOf(evs))
		}
	}
	firings := 0
	for _, ev := range e.QueryEvents("r1", true, nil) {
		if ev.Type == engine.EventFiring {
			firings++
		}
	}
	if firings != 1 {
		t.Fatalf("firing notifications=%d want 1", firings)
	}

	// Brief cold then hot again while recovering: returns to firing but
	// sends no new firing notification.
	send(e, tc.Add(11*30*time.Second), "cpu", 10)
	send(e, tc.Add(12*30*time.Second), "cpu", 99)
	if stateOf(e, "r1") != engine.StateFiring {
		t.Fatal("expected re-firing state")
	}
	firings = 0
	for _, ev := range e.QueryEvents("r1", true, nil) {
		if ev.Type == engine.EventFiring {
			firings++
		}
	}
	if firings != 1 {
		t.Fatalf("firing notifications after recover->firing=%d want still 1", firings)
	}
}

// TestRuleUpdateResetsState: a configuration change bumps version, resets
// runtime state and records a rule_reset audit event.
func TestRuleUpdateResetsState(t *testing.T) {
	e := newTestEngine()
	r := baseRule("r1", "cpu")
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	send(e, tc, "cpu", 99)
	if stateOf(e, "r1") != engine.StatePending {
		t.Fatal("expected pending before update")
	}

	updated := r
	updated.Threshold = 50
	if err := e.UpdateRule(updated); err != nil {
		t.Fatalf("update: %v", err)
	}
	rs, _ := e.GetRule("r1")
	if rs.Rule.Version != 2 {
		t.Fatalf("version=%d want 2", rs.Rule.Version)
	}
	if rs.State.State != engine.StateInactive {
		t.Fatalf("state after update=%s want inactive", rs.State.State)
	}
	if rs.State.HotSince != nil || rs.State.LastValue != nil {
		t.Fatal("runtime anchors must be cleared after config reset")
	}
	var resetEvents int
	for _, ev := range e.QueryEvents("r1", false, nil) {
		if ev.Type == engine.EventRuleReset {
			resetEvents++
			if ev.Version != 2 {
				t.Fatalf("rule_reset event version=%d want 2", ev.Version)
			}
		}
	}
	if resetEvents != 1 {
		t.Fatalf("rule_reset events=%d want 1", resetEvents)
	}

	// rule_reset must not leak into the notifications-only feed.
	notifs := e.QueryEvents("r1", true, nil)
	for _, ev := range notifs {
		if ev.Type == engine.EventRuleReset {
			t.Fatal("rule_reset leaked into notifications feed")
		}
	}

	// Update of a missing rule fails; invalid recovery band fails.
	if err := e.UpdateRule(baseRule("missing", "cpu")); err == nil {
		t.Fatal("update of missing rule must fail")
	}
	bad := updated
	bad.RecoveryThreshold = 100
	bad.HasRecovery = true
	if err := e.UpdateRule(bad); err == nil {
		t.Fatal("recovery threshold above threshold for >= must fail validation")
	}
}

// TestDownwardOperator exercises < with an upper recovery band.
func TestDownwardOperator(t *testing.T) {
	e := newTestEngine()
	r := engine.Rule{
		ID: "disk", Metric: "disk_free_gb", Operator: engine.OpLessThan,
		Threshold: 10, TriggerFor: engine.Duration{Duration: 30 * time.Second},
		RecoverFor:        engine.Duration{Duration: 30 * time.Second},
		NoDataFor:         engine.Duration{Duration: time.Minute},
		RecoveryThreshold: 20, HasRecovery: true, // 10..20 warm
	}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	send(e, tc, "disk_free_gb", 5)
	send(e, tc.Add(30*time.Second), "disk_free_gb", 4)
	if stateOf(e, "disk") != engine.StateFiring {
		t.Fatal("expected firing for low disk")
	}
	// Warm 15 keeps it open.
	send(e, tc.Add(60*time.Second), "disk_free_gb", 15)
	if stateOf(e, "disk") != engine.StateFiring {
		t.Fatal("warm value must keep alert firing")
	}
	// Cold 25 sustained resolves.
	send(e, tc.Add(90*time.Second), "disk_free_gb", 25)
	send(e, tc.Add(120*time.Second), "disk_free_gb", 26)
	if stateOf(e, "disk") != engine.StateInactive {
		t.Fatalf("state=%s want inactive after sustained cold", stateOf(e, "disk"))
	}
}

// TestImmediateDurations: trigger_for=0 fires on the first hot sample;
// recover_for=0 resolves on the first cold sample.
func TestImmediateDurations(t *testing.T) {
	e := newTestEngine()
	r := engine.Rule{
		ID: "imm", Metric: "m", Operator: engine.OpGreaterThan, Threshold: 1,
		TriggerFor: engine.Duration{}, RecoverFor: engine.Duration{},
		NoDataFor: engine.Duration{Duration: time.Minute},
	}
	if err := e.CreateRule(r); err != nil {
		t.Fatal(err)
	}
	rs, _ := e.GetRule("imm")
	if rs.Rule.TriggerFor.Duration != 0 || rs.Rule.RecoverFor.Duration != 0 {
		t.Fatalf("zero trigger/recover durations must mean immediate, got %s/%s",
			rs.Rule.TriggerFor.Duration, rs.Rule.RecoverFor.Duration)
	}
	evs := send(e, tc, "m", 2)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventFiring {
		t.Fatalf("first hot sample with trigger_for=0: %v want [firing]", got)
	}
	if stateOf(e, "imm") != engine.StateFiring {
		t.Fatal("want firing immediately")
	}
	evs = send(e, tc.Add(time.Second), "m", 0)
	if got := typesOf(evs); len(got) != 1 || got[0] != engine.EventResolved {
		t.Fatalf("first cold sample with recover_for=0: %v want [resolved]", got)
	}
}

// TestValidationErrorCases covers configuration rejections.
func TestValidationErrorCases(t *testing.T) {
	e := newTestEngine()
	bad := baseRule("bad", "m")
	bad.NoDataFor = engine.Duration{Duration: 0}
	if err := e.CreateRule(bad); err == nil {
		t.Fatal("zero no_data_for must be rejected")
	}
	bad2 := baseRule("bad2", "m")
	bad2.Operator = "=="
	if err := e.CreateRule(bad2); err == nil {
		t.Fatal("unsupported operator must be rejected")
	}
	good := baseRule("good", "m")
	if err := e.CreateRule(good); err != nil {
		t.Fatal(err)
	}
	if err := e.CreateRule(good); err == nil {
		t.Fatal("duplicate rule id must be rejected")
	}
	if err := e.DeleteRule("good"); err != nil {
		t.Fatal(err)
	}
	if err := e.DeleteRule("good"); err == nil {
		t.Fatal("deleting missing rule must fail")
	}
	if _, err := e.Tick(0); err == nil {
		t.Fatal("zero tick must fail")
	}
	if _, err := e.Tick(-time.Second); err == nil {
		t.Fatal("negative tick must fail")
	}
}

// TestMultipleRulesSameMetric: rules on one metric evaluate independently.
func TestMultipleRulesSameMetric(t *testing.T) {
	e := newTestEngine()
	warn := baseRule("warn", "cpu")
	warn.Threshold = 70
	crit := baseRule("crit", "cpu")
	crit.Threshold = 90
	if err := e.CreateRule(warn); err != nil {
		t.Fatal(err)
	}
	if err := e.CreateRule(crit); err != nil {
		t.Fatal(err)
	}
	send(e, tc, "cpu", 95)
	if stateOf(e, "warn") != engine.StatePending || stateOf(e, "crit") != engine.StatePending {
		t.Fatalf("both rules should be pending, got warn=%s crit=%s", stateOf(e, "warn"), stateOf(e, "crit"))
	}
	send(e, tc.Add(60*time.Second), "cpu", 95)
	if stateOf(e, "warn") != engine.StateFiring || stateOf(e, "crit") != engine.StateFiring {
		t.Fatalf("both should fire, got warn=%s crit=%s", stateOf(e, "warn"), stateOf(e, "crit"))
	}
	send(e, tc.Add(90*time.Second), "cpu", 75)
	// warn: 75 >= 70 still hot (no band); crit: 75 < 90 cold -> recovering.
	if stateOf(e, "warn") != engine.StateFiring {
		t.Fatalf("warn should stay firing, got %s", stateOf(e, "warn"))
	}
	if stateOf(e, "crit") != engine.StateRecovering {
		t.Fatalf("crit should recover, got %s", stateOf(e, "crit"))
	}
}
