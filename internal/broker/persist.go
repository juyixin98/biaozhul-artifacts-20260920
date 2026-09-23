package broker

import (
	"time"
)

// persistDebounce is how long the loop waits after a dirty signal before
// flushing; bursts of messages collapse into one disk write.
const persistDebounce = 200 * time.Millisecond

// persistLoop debounces durable-session snapshots. A final synchronous
// flush happens in Shutdown, so everything that ever marked a session
// dirty is on disk before the process exits (modulo OS write-back caches,
// which fsync would tighten but we leave to the containing filesystem).
func (b *Broker) persistLoop() {
	defer b.wg.Done()
	timer := time.NewTimer(1<<63 - 1)
	timer.Stop()
	pending := false
	for {
		select {
		case <-b.stopCh:
			timer.Stop()
			return
		case <-b.wakeCh:
			if !pending {
				pending = true
				timer.Reset(persistDebounce)
			}
		case <-timer.C:
			if pending {
				_ = b.flush()
				pending = false
			}
		}
	}
}

// flush serialises all durable sessions and rewrites the store file.
func (b *Broker) flush() error {
	if b.store == nil || b.store.path == "" {
		return nil
	}
	b.mu.Lock()
	sf := &snapshotFile{Version: snapshotVersion}
	for _, s := range b.sessions {
		if !s.Durable {
			continue
		}
		snap := sessionSnapshot{
			ClientID: s.ClientID,
			Subs:     map[string]byte{},
			NextPID:  s.nextPID,
		}
		for f, q := range s.subs {
			snap.Subs[f] = q
		}
		for _, m := range s.pending {
			snap.Pending = append(snap.Pending, storedMessage{
				Topic: m.Topic, QoS: m.QoS, Payload: append([]byte(nil), m.Payload...),
				PacketID: m.PacketID,
			})
		}
		for _, pid := range s.inflightOrder {
			m := s.inflight[pid]
			if m == nil {
				continue
			}
			snap.Inflight = append(snap.Inflight, storedMessage{
				Topic: m.Topic, QoS: m.QoS, Payload: append([]byte(nil), m.Payload...),
				PacketID: m.PacketID,
			})
		}
		for pid := range s.seenInbound {
			snap.SeenInbound = append(snap.SeenInbound, pid)
		}
		sf.Sessions = append(sf.Sessions, snap)
		s.dirty = false
	}
	b.mu.Unlock()
	return b.store.save(sf)
}
