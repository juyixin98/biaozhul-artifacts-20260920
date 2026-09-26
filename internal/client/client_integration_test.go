package client_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"gracefulshutdown/internal/app"
	"gracefulshutdown/internal/client"
)

func startServer(t *testing.T) (*app.App, *client.Client) {
	t.Helper()
	cfg := app.Config{
		PublicAddr:    "127.0.0.1:0",
		AdminAddr:     "127.0.0.1:0",
		RejectWindow:  100 * time.Millisecond,
		DrainTimeout:  800 * time.Millisecond,
		CancelTimeout: 800 * time.Millisecond,
		CloseTimeout:  time.Second,
	}
	a := app.New(cfg)
	if err := a.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	t.Cleanup(func() {
		select {
		case <-a.Coordinator().Done():
		default:
			a.Shutdown()
		}
	})
	return a, client.New(a.PublicURL(), a.AdminURL())
}

func TestClientLongRequestCompletes(t *testing.T) {
	t.Parallel()
	_, c := startServer(t)
	ctx := context.Background()
	if _, err := c.SetFault(ctx, 50*time.Millisecond, false, false); err != nil {
		t.Fatalf("set fault: %v", err)
	}
	obs, err := c.SendWork(ctx)
	if err != nil {
		t.Fatalf("send work: %v", err)
	}
	if obs.StatusCode != 200 || obs.Outcome != "completed" {
		t.Fatalf("obs=%+v, want 200/completed", obs)
	}
}

func TestClientProbesHealthyBeforeShutdown(t *testing.T) {
	t.Parallel()
	_, c := startServer(t)
	ctx := context.Background()
	ready, err := c.Probe(ctx, "readyz")
	if err != nil || ready.StatusCode != 200 || ready.Ready == nil || !*ready.Ready {
		t.Fatalf("ready probe=%+v err=%v", ready, err)
	}
	live, err := c.Probe(ctx, "livez")
	if err != nil || live.StatusCode != 200 || live.Alive == nil || !*live.Alive {
		t.Fatalf("live probe=%+v err=%v", live, err)
	}
}

func TestClientSpawnAndStreamComplete(t *testing.T) {
	t.Parallel()
	_, c := startServer(t)
	ctx := context.Background()

	bg, err := c.SpawnBackground(ctx, 100*time.Millisecond)
	if err != nil || bg.StatusCode != 202 || bg.Outcome != "accepted" {
		t.Fatalf("bg obs=%+v err=%v", bg, err)
	}

	stream, err := c.OpenStream(ctx, 300*time.Millisecond)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	if stream.Outcome != "completed" {
		t.Fatalf("stream outcome=%s, want completed", stream.Outcome)
	}
	if !strings.Contains(stream.Headers["Content-Type"], "text/event-stream") {
		t.Fatalf("content-type=%q", stream.Headers["Content-Type"])
	}
	if len(stream.Events) == 0 {
		t.Fatal("expected SSE events")
	}
}

func TestClientTriggerShutdownReturnsReportAndCancelsHungWork(t *testing.T) {
	t.Parallel()
	a, c := startServer(t)
	ctx := context.Background()
	if _, err := c.SetFault(ctx, 0, false, true); err != nil {
		t.Fatalf("set fault: %v", err)
	}

	longDone := make(chan string, 1)
	go func() {
		obs, _ := c.SendWork(ctx)
		longDone <- obs.Outcome
	}()
	time.Sleep(120 * time.Millisecond)

	raw, obs, err := c.TriggerShutdown(ctx, 2)
	if err != nil {
		t.Fatalf("trigger: %v", err)
	}
	if obs.StatusCode != 200 {
		t.Fatalf("trigger status=%d", obs.StatusCode)
	}
	if !strings.Contains(string(raw), `"signalsReceived": 2`) {
		t.Fatalf("report missing 2 signals: %s", raw)
	}

	select {
	case out := <-longDone:
		if out != "cancelled" {
			t.Fatalf("hung work outcome=%s, want cancelled", out)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("hung work never resolved")
	}
	select {
	case <-a.Coordinator().Done():
	case <-time.After(3 * time.Second):
		t.Fatal("server did not finish shutdown")
	}
}
