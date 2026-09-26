package main

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

// 场景 1：基本重放——同键同正文第二次请求原样返回，本地副作用仅一次。
func scenarioReplay(r *runner) {
	key := "key-replay-0001"
	body := depositBody("acct-replay", 100)

	resp1 := r.deposit("first request", key, body, nil)
	resp2 := r.deposit("retry same key+body", key, body, nil)

	r.check("first request 200 created",
		resp1.StatusCode == http.StatusOK && replayHeader(resp1) == "created",
		"status=%d replay=%q body=%s", resp1.StatusCode, replayHeader(resp1), resp1.Body)

	r.check("retry 200 replayed",
		resp2.StatusCode == http.StatusOK && replayHeader(resp2) == "replayed",
		"status=%d replay=%q", resp2.StatusCode, replayHeader(resp2))

	r.check("same tx_id on replay", txID(resp1) != "" && txID(resp1) == txID(resp2),
		"first=%q second=%q", txID(resp1), txID(resp2))

	led := r.ledger()
	r.check("exactly one committed local effect", len(led.Effects) == 1,
		"ledger effects=%d", len(led.Effects))
	r.check("balance equals single deposit", balanceOf(led, "acct-replay") == 100,
		"balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("external system called exactly once (replay does not re-call)",
		intFromAny(audit["calls"]) == 1 && intFromAny(audit["calls"]) == lenEvents(audit),
		"audit=%v", audit)
}

// 场景 2：同键不同正文——409 冲突，不产生第二笔副作用。
func scenarioConflict(r *runner) {
	key := "key-conflict-0002"
	body1 := depositBody("acct-c", 100)
	body2 := depositBody("acct-c", 250) // 金额不同 → 摘要不同

	resp1 := r.deposit("original request", key, body1, nil)
	resp2 := r.deposit("same key, different amount", key, body2, nil)
	resp3 := r.deposit("different body repeated", key, body2, nil)

	r.check("original 200", resp1.StatusCode == http.StatusOK, "status=%d", resp1.StatusCode)
	r.check("different body -> 409 conflict", resp2.StatusCode == http.StatusConflict,
		"status=%d body=%s", resp2.StatusCode, resp2.Body)
	r.check("conflict is stable on repeat", resp3.StatusCode == http.StatusConflict,
		"status=%d", resp3.StatusCode)

	led := r.ledger()
	r.check("conflict created no extra effect", len(led.Effects) == 1,
		"ledger effects=%d", len(led.Effects))
	r.check("balance unchanged by conflicting request", balanceOf(led, "acct-c") == 100,
		"balances=%v", led.Raw["balances"])
}

// 场景 3：处理中的重复请求——先得到明确的 202/等待后重放，而非盲重。
func scenarioInProgress(r *runner) {
	key := "key-inprogress-0003"
	body := depositBody("acct-ip", 50)
	headers := map[string]string{"X-Fault-Delay-Before-Commit-Ms": "400"}

	var first, second Resp
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { // 首个请求：占位后在提交前停留 400ms
		defer wg.Done()
		first = r.deposit("slow first request", key, body, headers)
	}()
	time.Sleep(150 * time.Millisecond) // 确保首个请求已占位
	go func() {                        // 重复请求：应当等待到首个请求提交，然后拿到重放结果
		defer wg.Done()
		second = r.deposit("concurrent duplicate while processing", key, body, nil)
	}()
	wg.Wait()

	r.check("first request 200 created",
		first.StatusCode == http.StatusOK && replayHeader(first) == "created",
		"status=%d replay=%q", first.StatusCode, replayHeader(first))
	r.check("duplicate eventually 200 replayed (not a blind retry, not 5xx)",
		second.StatusCode == http.StatusOK && replayHeader(second) == "replayed",
		"status=%d replay=%q body=%s", second.StatusCode, replayHeader(second), second.Body)
	r.check("both saw the same tx_id", txID(first) != "" && txID(first) == txID(second),
		"%q vs %q", txID(first), txID(second))

	led := r.ledger()
	r.check("exactly one local effect", len(led.Effects) == 1, "effects=%d", len(led.Effects))

	audit := r.auditInspect()
	r.check("external system called once", intFromAny(audit["calls"]) == 1, "audit=%v", audit)
}

// 场景 4：N 个并发同键请求——只有一个执行，其余全部重放，本地副作用仅一次。
func scenarioConcurrent(r *runner) {
	key := "key-concurrent-0004"
	body := depositBody("acct-conc", 77)

	resps := make([]Resp, concurrencyN)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < concurrencyN; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-start
			resps[idx] = r.deposit("concurrent attempt", key, body, nil)
		}(i)
	}
	close(start)
	wg.Wait()

	created, replayed, other := 0, 0, 0
	var firstTx string
	allSameTx := true
	for _, resp := range resps {
		switch {
		case resp.StatusCode == http.StatusOK && replayHeader(resp) == "created":
			created++
			firstTx = txID(resp)
		case resp.StatusCode == http.StatusOK && replayHeader(resp) == "replayed":
			replayed++
		default:
			other++
		}
	}
	for _, resp := range resps {
		if txID(resp) != "" && firstTx != "" && txID(resp) != firstTx {
			allSameTx = false
		}
	}

	r.check("all 24 concurrent requests got 200", created+replayed == concurrencyN,
		"created=%d replayed=%d other=%d", created, replayed, other)
	r.check("exactly one request executed (created)", created == 1, "created=%d", created)
	r.check("the other 23 were replayed", replayed == concurrencyN-1, "replayed=%d", replayed)
	r.check("every client observed the same tx_id", allSameTx && firstTx != "", "tx=%q", firstTx)

	led := r.ledger()
	r.check("exactly one committed local effect under concurrency",
		len(led.Effects) == 1, "effects=%d", len(led.Effects))
	r.check("balance is 77, not 77*N", balanceOf(led, "acct-conc") == 77,
		"balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("external system called once under concurrency",
		intFromAny(audit["calls"]) == 1, "audit=%v", audit)
}

// 场景 5：提交前断线——占位遗留；TTL 前重试得到 202，TTL 后重试成功；
// 本地副作用仅一次，外部调用可能两次（本场景即为两次）。
func scenarioDisconnectBefore(r *runner) {
	key := "key-disconnect-before-0005"
	body := depositBody("acct-db", 300)
	hdr := map[string]string{"X-Fault-Before-Commit-Disconnect": "1"}

	resp1 := r.deposit("connection dropped before commit", key, body, hdr)
	r.check("client observed a transport failure (no response)",
		resp1.IsDisconnect(), "transport_error=%q", resp1.TransportError)

	// TTL 未到：占位仍属于"死去"的尝试，重复请求得到明确的 202，而不会重复执行。
	resp2 := r.deposit("retry while lease alive", key, body, nil)
	r.check("retry before TTL expiry is told in-progress (202), no second effect",
		resp2.StatusCode == http.StatusAccepted, "status=%d body=%s", resp2.StatusCode, resp2.Body)

	r.advanceClock(ttlMS(r) + 200) // 拨过占位 TTL

	resp3 := r.waitForFinal("retry after TTL expiry", key, body, nil)
	r.check("retry after TTL expiry succeeds 200",
		resp3.StatusCode == http.StatusOK, "status=%d body=%s", resp3.StatusCode, resp3.Body)

	resp4 := r.deposit("another retry replays", key, body, nil)
	r.check("further retry replays the saved result",
		resp4.StatusCode == http.StatusOK && replayHeader(resp4) == "replayed",
		"status=%d replay=%q", resp4.StatusCode, replayHeader(resp4))

	led := r.ledger()
	r.check("LOCAL side effect committed exactly once", len(led.Effects) == 1,
		"effects=%d", len(led.Effects))
	r.check("balance is 300", balanceOf(led, "acct-db") == 300, "balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("external system MAY be called twice (not guaranteed exactly-once)",
		lenEvents(audit) == 2, "external events=%d (local effects=1)", lenEvents(audit))
	r.sr.Evidence["note"] = "第一次外部调用发生在断线前；TTL 回收后重试再次调用——本地事务仍只一笔"
}

// 场景 6：提交后断线——事务已落盘，重试只重放，本地与外部都仅一次。
func scenarioDisconnectAfter(r *runner) {
	key := "key-disconnect-after-0006"
	body := depositBody("acct-da", 900)
	hdr := map[string]string{"X-Fault-After-Commit-Disconnect": "1"}

	resp1 := r.deposit("connection dropped after commit", key, body, hdr)
	r.check("client observed a transport failure",
		resp1.IsDisconnect(), "transport_error=%q", resp1.TransportError)

	resp2 := r.waitForFinal("retry after post-commit disconnect", key, body, nil)
	r.check("retry replays the already-durable result",
		resp2.StatusCode == http.StatusOK && replayHeader(resp2) == "replayed",
		"status=%d replay=%q", resp2.StatusCode, replayHeader(resp2))
	r.check("replayed tx_id is present", txID(resp2) != "", "body=%s", resp2.Body)

	led := r.ledger()
	r.check("exactly one local effect", len(led.Effects) == 1, "effects=%d", len(led.Effects))
	r.check("balance is 900", balanceOf(led, "acct-da") == 900, "balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("external system called once (no re-execution after commit)",
		intFromAny(audit["calls"]) == 1, "audit=%v", audit)
}

// 场景 7：提交前进程崩溃——重启后 WAL 中占位仍在；TTL 后重试成功，本地仅一笔。
func scenarioCrash(r *runner) {
	key := "key-crash-0007"
	body := depositBody("acct-crash", 150)
	hdr := map[string]string{"X-Fault-Crash-Before-Commit": "1"}

	resp1 := r.deposit("server crashes before commit", key, body, hdr)
	r.check("client observed transport failure (process exited)",
		resp1.IsDisconnect(), "transport_error=%q", resp1.TransportError)

	if err := r.env.RestartBiz(r.ctx()); err != nil {
		r.check("business server restarts", false, "restart error: %v", err)
		return
	}
	r.check("business server restarts", true, "")

	// 崩溃前 pending 已 fsync：重启后记录必须还在（状态 pending）。
	code, view := r.get("/v1/idempotency/" + key)
	r.check("pending lease survived crash via WAL",
		code == http.StatusOK && view["status"] == "pending", "code=%d view=%v", code, view)

	resp2 := r.deposit("retry right after restart (lease alive)", key, body, nil)
	r.check("retry before TTL still gets 202", resp2.StatusCode == http.StatusAccepted,
		"status=%d", resp2.StatusCode)

	r.advanceClock(ttlMS(r) + 200)
	resp3 := r.waitForFinal("retry after TTL post-restart", key, body, nil)
	r.check("retry after TTL succeeds", resp3.StatusCode == http.StatusOK,
		"status=%d body=%s", resp3.StatusCode, resp3.Body)

	led := r.ledger()
	r.check("exactly one local effect after crash+recovery",
		len(led.Effects) == 1, "effects=%d", len(led.Effects))
	r.check("balance is 150", balanceOf(led, "acct-crash") == 150, "balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("external system survived biz crash and holds pre-crash event",
		lenEvents(audit) >= 1, "audit=%v", audit)
	r.sr.Evidence["note"] = "业务进程被 SIGKILL；独立的假审计进程存活，保留了崩溃前的外部事件"
}

// 场景 8：提交成功后崩溃重启——结果与副作用都从 WAL 恢复并重放。
func scenarioReplayAfterRestart(r *runner) {
	key := "key-restart-replay-0008"
	body := depositBody("acct-rr", 42)

	resp1 := r.deposit("successful deposit", key, body, nil)
	r.check("first request 200", resp1.StatusCode == http.StatusOK, "status=%d", resp1.StatusCode)
	txBefore := txID(resp1)

	if err := r.env.RestartBiz(r.ctx()); err != nil {
		r.check("business server restarts", false, "%v", err)
		return
	}
	r.check("business server restarts", true, "")

	led := r.ledger()
	r.check("effect recovered from WAL after restart", len(led.Effects) == 1,
		"effects=%d", len(led.Effects))
	r.check("balance recovered from WAL", balanceOf(led, "acct-rr") == 42,
		"balances=%v", led.Raw["balances"])

	resp2 := r.deposit("retry after restart replays", key, body, nil)
	r.check("retry after restart replays saved response",
		resp2.StatusCode == http.StatusOK && replayHeader(resp2) == "replayed" && txID(resp2) == txBefore,
		"status=%d replay=%q tx=%q want=%q", resp2.StatusCode, replayHeader(resp2), txID(resp2), txBefore)

	audit := r.auditInspect()
	r.check("external system not called again by replay",
		intFromAny(audit["calls"]) == 1, "audit=%v", audit)
}

// 场景 9：外部系统"已落账但响应丢失"——重试导致外部两条事件，
// 而本地账本仍只有一笔：明确不保证任意外部调用恰好一次。
func scenarioExternalDuplicate(r *runner) {
	r.auditFaults(map[string]any{"record_then_fail_next_n": 1})

	key := "key-external-dup-0009"
	body := depositBody("acct-ext", 500)

	resp1 := r.deposit("first call: external persists then returns 503", key, body, nil)
	r.check("first attempt surfaces a retryable upstream error",
		resp1.StatusCode == http.StatusBadGateway,
		"status=%d body=%s", resp1.StatusCode, resp1.Body)

	resp2 := r.waitForFinal("retry after ambiguous external failure", key, body, nil)
	r.check("retry succeeds 200", resp2.StatusCode == http.StatusOK,
		"status=%d body=%s", resp2.StatusCode, resp2.Body)

	led := r.ledger()
	r.check("LOCAL transaction happened exactly once", len(led.Effects) == 1,
		"local effects=%d", len(led.Effects))
	r.check("local balance is 500 (not 1000)", balanceOf(led, "acct-ext") == 500,
		"balances=%v", led.Raw["balances"])

	audit := r.auditInspect()
	r.check("EXTERNAL system received TWO events (exactly-once is NOT provided across systems)",
		lenEvents(audit) == 2, "external events=%d", lenEvents(audit))
	r.sr.Evidence["boundary"] =
		"幂等层的原子单位是本地 WAL 事务（结果+本地副作用）；外部 HTTP 调用无法纳入该事务，" +
			"在'已处理但响应丢失'时调用方无法分辨，重试即产生外部重复。"
}

// ---- 场景辅助 ----

// ctx 用于重启子进程：使用 Background，避免与一次性请求的超时绑定。
func (r *runner) ctx() context.Context { return context.Background() }

func ttlMS(r *runner) int {
	return int(r.env.ttl / time.Millisecond)
}

func balanceOf(l ledgerView, account string) int64 {
	balances, ok := l.Raw["balances"].(map[string]any)
	if !ok {
		return -1
	}
	return int64(intFromAny(balances[account]))
}

func lenEvents(m map[string]any) int {
	ev, ok := m["events"].([]any)
	if !ok {
		return -1
	}
	return len(ev)
}

func intFromAny(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return -1
}
