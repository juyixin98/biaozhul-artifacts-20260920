package faultclient

import (
	"context"
	"net/http"
	"testing"
	"time"

	"cbhalfopen/internal/breaker"
	"cbhalfopen/internal/fakeupstream"
	"cbhalfopen/internal/vclock"
)

type fixture struct {
	vc     *vclock.VirtualClock
	fake   *fakeupstream.Fake
	client *Client
	b      *breaker.Breaker
}

func newFixture(t *testing.T, cfg Config) fixture {
	t.Helper()
	vc := vclock.NewVirtual(time.UnixMilli(0).UTC())
	fake := fakeupstream.New(vc, fakeupstream.ModeOK)
	client := New(fake, vc, cfg)
	b, err := breaker.New(breaker.Config{
		SlidingWindowSize: 10,
		MinRequests:       5,
		FailureThreshold:  0.5,
		OpenCooldown:      5 * time.Second,
		HalfOpenMaxProbes: 3,
		RequiredSuccesses: 2,
		Clock:             vc,
	})
	if err != nil {
		t.Fatal(err)
	}
	return fixture{vc: vc, fake: fake, client: client, b: b}
}

func (f fixture) permit() *breaker.Permit {
	p, err := f.b.Allow()
	if err != nil {
		panic(err)
	}
	return p
}

func TestSuccessClassified(t *testing.T) {
	fx := newFixture(t, Config{})
	res := fx.client.Do(context.Background(), fx.permit())
	if res.Outcome != breaker.OutcomeSuccess || res.StatusCode != http.StatusOK {
		t.Fatalf("result = %+v", res)
	}
	if snap := fx.b.Snapshot(); snap.Counters.Successes != 1 {
		t.Fatalf("successes = %d", snap.Counters.Successes)
	}
}

func TestServerFailureClassified(t *testing.T) {
	fx := newFixture(t, Config{})
	fx.fake.SetMode(fakeupstream.ModeFail)
	res := fx.client.Do(context.Background(), fx.permit())
	if res.Outcome != breaker.OutcomeFailure || res.StatusCode != http.StatusInternalServerError {
		t.Fatalf("result = %+v", res)
	}
}

func TestInjectedTransportFailure(t *testing.T) {
	fx := newFixture(t, Config{FailNextN: 2})
	for i := 1; i <= 2; i++ {
		res := fx.client.Do(context.Background(), fx.permit())
		if res.Outcome != breaker.OutcomeFailure || res.Reason != "injected_transport_error" {
			t.Fatalf("call %d = %+v", i, res)
		}
	}
	// Budget exhausted: next call goes through and succeeds.
	res := fx.client.Do(context.Background(), fx.permit())
	if res.Outcome != breaker.OutcomeSuccess {
		t.Fatalf("third call = %+v, want success", res)
	}
}

func TestFailEveryNth(t *testing.T) {
	fx := newFixture(t, Config{FailEveryNth: 3})
	var outcomes []breaker.Outcome
	for i := 0; i < 6; i++ {
		r := fx.client.Do(context.Background(), fx.permit())
		outcomes = append(outcomes, r.Outcome)
	}
	want := []breaker.Outcome{
		breaker.OutcomeSuccess, breaker.OutcomeSuccess, breaker.OutcomeFailure,
		breaker.OutcomeSuccess, breaker.OutcomeSuccess, breaker.OutcomeFailure,
	}
	for i := range want {
		if outcomes[i] != want[i] {
			t.Fatalf("outcomes = %v, want %v", outcomes, want)
		}
	}
}

func TestInjectFailuresAddsBudget(t *testing.T) {
	fx := newFixture(t, Config{})
	fx.client.InjectFailures(1)
	if r := fx.client.Do(context.Background(), fx.permit()); r.Outcome != breaker.OutcomeFailure {
		t.Fatalf("injected call = %+v", r)
	}
	if r := fx.client.Do(context.Background(), fx.permit()); r.Outcome != breaker.OutcomeSuccess {
		t.Fatalf("following call = %+v", r)
	}
}

func TestVirtualTimeoutIsFailure(t *testing.T) {
	fx := newFixture(t, Config{Timeout: 100 * time.Millisecond})
	fx.fake.SetMode(fakeupstream.ModeHang)

	done := make(chan Result, 1)
	go func() {
		done <- fx.client.Do(context.Background(), fx.permit())
	}()
	waitForPending(fx, 1)

	fx.vc.Advance(100 * time.Millisecond)
	select {
	case res := <-done:
		if res.Outcome != breaker.OutcomeFailure || res.Reason != "timeout" {
			t.Fatalf("timed out call = %+v", res)
		}
		if res.ElapsedVirt != 100*time.Millisecond {
			t.Fatalf("elapsed = %v, want 100ms", res.ElapsedVirt)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("virtual timeout did not unblock the call")
	}
	if snap := fx.b.Snapshot(); snap.Counters.Failures != 1 {
		t.Fatalf("failures = %d, want 1", snap.Counters.Failures)
	}
	fx.fake.ReleaseAll(fakeupstream.ModeFail) // cleanup parked attempt
}

func TestCallerCancelIsCanceledNotFailure(t *testing.T) {
	fx := newFixture(t, Config{Timeout: time.Hour})
	fx.fake.SetMode(fakeupstream.ModeHang)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan Result, 1)
	go func() {
		done <- fx.client.Do(ctx, fx.permit())
	}()
	waitForPending(fx, 1)

	cancel()
	select {
	case res := <-done:
		if res.Outcome != breaker.OutcomeCanceled || res.Reason != "caller_canceled" {
			t.Fatalf("canceled call = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("cancel did not unblock the call")
	}
	snap := fx.b.Snapshot()
	if snap.Counters.Canceled != 1 {
		t.Fatalf("canceled = %d, want 1", snap.Counters.Canceled)
	}
	if snap.Counters.Failures != 0 {
		t.Fatalf("cancel counted as failure: %d", snap.Counters.Failures)
	}
	fx.fake.ReleaseAll(fakeupstream.ModeFail) // cleanup
}

func TestWithTimeoutOverridesConfig(t *testing.T) {
	fx := newFixture(t, Config{Timeout: time.Hour})
	fx.fake.SetMode(fakeupstream.ModeHang)

	done := make(chan Result, 1)
	go func() {
		done <- fx.client.Do(context.Background(), fx.permit(), WithTimeout(50*time.Millisecond))
	}()
	waitForPending(fx, 1)
	fx.vc.Advance(50 * time.Millisecond)
	select {
	case res := <-done:
		if res.Outcome != breaker.OutcomeFailure || res.Reason != "timeout" {
			t.Fatalf("override timeout call = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("override timeout never fired")
	}
	fx.fake.ReleaseAll(fakeupstream.ModeFail)
}

func TestProbeMetadataRecorded(t *testing.T) {
	fx := newFixture(t, Config{})
	for i := 0; i < 5; i++ {
		fx.fake.SetMode(fakeupstream.ModeFail)
		fx.client.Do(context.Background(), fx.permit())
	}
	fx.vc.Advance(5 * time.Second)
	fx.fake.SetMode(fakeupstream.ModeOK)

	res := fx.client.Do(context.Background(), fx.permit())
	if !res.Probe || res.Generation != 2 {
		t.Fatalf("probe result = %+v, want probe gen 2", res)
	}
}

func TestSetConfigResetsFailBudget(t *testing.T) {
	fx := newFixture(t, Config{FailNextN: 5})
	fx.client.SetConfig(Config{}) // reset clears the 5-error budget
	if r := fx.client.Do(context.Background(), fx.permit()); r.Outcome != breaker.OutcomeSuccess {
		t.Fatalf("after reset call = %+v", r)
	}
}

func waitForPending(fx fixture, n int) {
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(fx.fake.Pending()) >= n {
			return
		}
	}
	panic("calls never parked")
}
