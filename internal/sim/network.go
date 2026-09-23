package sim

// deliver routes one envelope to the addressed node or to the router.
func (s *Sim) deliver(to, from string, m message) {
	switch to {
	case routerAddr:
		s.router.handle(from, m.v)
	default:
		n := s.nodes[to]
		if n == nil {
			return
		}
		// A decommission is an idempotent power-off: a node that is already
		// powered off must still ACK the repeated command, otherwise a single
		// lost ACK wedges the coordinator forever. All other traffic to a
		// dead node is silently lost.
		if _, isDecommission := m.v.(msgDecommission); !isDecommission && !n.alive {
			return
		}
		s.nodeHandle(n, from, m.v)
	}
}

// send transmits one message through the lossy network. Independently per
// send it may be dropped or delivered twice; every delivered copy gets its own
// randomized delay, which is what produces out-of-order arrivals.
func (s *Sim) send(from, to string, m message) {
	s.result.Stats.MessagesSent++
	if s.rng.Float64() < s.cfg.Network.DropRate {
		s.result.Stats.MessagesDropped++
		return
	}
	copies := 1
	if s.rng.Float64() < s.cfg.Network.DupRate {
		copies = 2
		s.result.Stats.MessagesDuplicated++
	}
	for i := 0; i < copies; i++ {
		delay := s.cfg.Network.BaseDelayMs
		if s.cfg.Network.JitterMs > 0 {
			delay += s.rng.Int63n(s.cfg.Network.JitterMs + 1)
		}
		mm := m
		tt := to
		s.after(delay, func() {
			s.result.Stats.MessagesDelivered++
			s.deliver(tt, from, mm)
		})
	}
}
