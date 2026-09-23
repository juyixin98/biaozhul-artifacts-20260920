package runner

import "twopcsim/internal/twopc"

// netSend 是唯一的消息出口：确定性地施加丢弃、重复、乱序延迟，
// 然后把“消息到达”事件放进调度器。事件 owner 为接收方——
// 接收方崩溃时这些在途消息随 CancelOwner 一并消失。
func (r *Runner) netSend(from string, m twopc.Message) {
	if m.From == "" {
		m.From = from
	}
	n := r.sc.Network
	r.trace = append(r.trace, TraceEntry{
		Tick:   r.sched.Now(),
		Node:   from,
		Kind:   "net.send",
		TxnID:  m.TxnID,
		Detail: map[string]any{"type": m.Type, "to": m.To},
	})

	// 丢弃
	if n.LossRate > 0 && r.sched.Rng().Float64() < n.LossRate {
		r.trace = append(r.trace, TraceEntry{
			Tick:   r.sched.Now(),
			Node:   from,
			Kind:   "net.dropped",
			TxnID:  m.TxnID,
			Detail: map[string]any{"type": m.Type, "to": m.To},
		})
		return
	}

	// 重复：除原始副本外再投递一份。
	if n.DuplicateRate > 0 && r.sched.Rng().Float64() < n.DuplicateRate {
		r.trace = append(r.trace, TraceEntry{
			Tick:   r.sched.Now(),
			Node:   from,
			Kind:   "net.duplicated",
			TxnID:  m.TxnID,
			Detail: map[string]any{"type": m.Type, "to": m.To},
		})
		r.scheduleDelivery(m, true)
	}
	r.scheduleDelivery(m, false)
}

func (r *Runner) scheduleDelivery(m twopc.Message, duplicate bool) {
	n := r.sc.Network
	span := n.MaxDelay - n.MinDelay
	delay := n.MinDelay
	if span > 0 {
		delay += r.sched.Rng().Int63n(span + 1)
	}
	// 乱序：给一部分消息附加较长延迟，使其晚于后发出的消息到达。
	if n.ReorderRate > 0 && r.sched.Rng().Float64() < n.ReorderRate && n.ReorderDelay > 0 {
		delay += n.ReorderDelay
	}
	to := m.To
	kind := "message"
	r.sched.ScheduleFrom(to, m.From, kind, delay, func() {
		if r.down[to] {
			r.trace = append(r.trace, TraceEntry{
				Tick:   r.sched.Now(),
				Node:   to,
				Kind:   "net.lostNodeDown",
				TxnID:  m.TxnID,
				Detail: map[string]any{"type": m.Type, "from": m.From},
			})
			return
		}
		r.trace = append(r.trace, TraceEntry{
			Tick:   r.sched.Now(),
			Node:   to,
			Kind:   "net.deliver",
			TxnID:  m.TxnID,
			Detail: map[string]any{"type": m.Type, "from": m.From, "duplicate": duplicate},
		})
		r.nodes[to].OnMessage(m)
	})
}
