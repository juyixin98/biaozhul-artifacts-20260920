package event_test

import (
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"worksteal/internal/event"
)

func TestEmitAndSubscribe(t *testing.T) {
	bus := event.NewBus("ex1")
	sub, history := bus.Subscribe(8)
	defer sub.Close()
	if len(history) != 0 {
		t.Fatalf("fresh bus history=%d", len(history))
	}
	for i := 0; i < 5; i++ {
		bus.Emit(time.Unix(int64(i), 0), event.KindTaskCompleted, "t", "", 1, nil, nil)
	}
	got := drain(sub, 5, time.Second)
	if len(got) != 5 {
		t.Fatalf("got %d events", len(got))
	}
	for i, ev := range got {
		if ev.Seq != int64(i+1) || ev.Executor != "ex1" || ev.Kind != event.KindTaskCompleted {
			t.Fatalf("bad event %+v at %d", ev, i)
		}
	}
}

func TestHistorySnapshot(t *testing.T) {
	bus := event.NewBus("ex2", event.WithCapacity(3))
	for i := 0; i < 10; i++ {
		bus.Emit(time.Now(), event.KindTaskSpawned, "id", "", -1, nil, nil)
	}
	_, history := bus.Subscribe(4)
	// 容量 3 + DropOldest：历史只保留最近 3 条。
	if len(history) != 3 {
		t.Fatalf("history=%d want 3", len(history))
	}
}

func TestDropNewest(t *testing.T) {
	bus := event.NewBus("ex3", event.WithCapacity(3), event.WithOverflow(event.DropNewest))
	for i := 0; i < 10; i++ {
		bus.Emit(time.Now(), event.KindTaskSpawned, "id", "", -1, nil, nil)
	}
	_, history := bus.Subscribe(4)
	// 容量 3 + DropNewest：保留最旧的 3 条（历史前缀），新事件被丢。
	if len(history) != 3 {
		t.Fatalf("history=%d want 3", len(history))
	}
	if bus.Stats().Dropped < 7 {
		t.Fatalf("dropped=%d want >=7", bus.Stats().Dropped)
	}
}

func TestSubscriberOverflowDropsNotBlocks(t *testing.T) {
	bus := event.NewBus("ex4")
	sub, _ := bus.Subscribe(1) // 极小订阅缓冲
	defer sub.Close()
	// 大量发射：总线不得阻塞；慢订阅者丢事件。
	done := make(chan struct{})
	go func() {
		for i := 0; i < 100000; i++ {
			bus.Emit(time.Now(), event.KindTaskCompleted, "x", "", -1, nil, nil)
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Emit blocked on slow subscriber")
	}
	st := bus.Stats()
	if st.Dropped == 0 {
		t.Fatalf("expected drops for slow subscriber, stats=%+v", st)
	}
}

func TestErrField(t *testing.T) {
	bus := event.NewBus("ex5")
	sub, _ := bus.Subscribe(4)
	defer sub.Close()
	bus.Emit(time.Now(), event.KindTaskFailed, "t", "", 0, errors.New("boom"), nil)
	got := drain(sub, 1, time.Second)
	if len(got) != 1 || got[0].Err != "boom" {
		t.Fatalf("event=%+v", got)
	}
}

func TestCloseUnsubscribes(t *testing.T) {
	bus := event.NewBus("ex6")
	sub, _ := bus.Subscribe(4)
	sub.Close()
	if bus.Stats().Subscribers != 0 {
		t.Fatalf("subscribers=%d after close", bus.Stats().Subscribers)
	}
	// 重复关闭安全。
	sub.Close()
	// 关闭后通道已关闭。
	if _, ok := <-sub.C(); ok {
		t.Fatal("channel not closed after Close")
	}
}

func TestSinkFunc(t *testing.T) {
	var n atomic.Int32
	var _ event.Sink = event.SinkFunc(func(ev event.Event) { n.Add(1) })
}

func drain(sub *event.Subscription, want int, max time.Duration) []event.Event {
	var out []event.Event
	deadline := time.After(max)
	for len(out) < want {
		select {
		case ev, ok := <-sub.C():
			if !ok {
				return out
			}
			out = append(out, ev)
		case <-deadline:
			return out
		}
	}
	return out
}
