package broker

import (
	"sort"
)

// SessionInfo is the JSON view of one session.
type SessionInfo struct {
	ClientID      string          `json:"client_id"`
	Durable       bool            `json:"durable"`
	Online        bool            `json:"online"`
	Subscriptions map[string]byte `json:"subscriptions"`
	Pending       int             `json:"pending"`
	Inflight      []uint16        `json:"inflight"`
	NextPacketID  uint16          `json:"next_packet_id"`
}

// Stats returns a point-in-time copy of the counters.
func (b *Broker) Stats() Stats {
	b.mu.Lock()
	defer b.mu.Unlock()
	st := b.stats
	st.Sessions = len(b.sessions)
	for _, s := range b.sessions {
		if s.conn != nil {
			st.Online++
		}
		st.Inflight += len(s.inflight)
	}
	return st
}

// Sessions lists every session, sorted by client id.
func (b *Broker) Sessions() []SessionInfo {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]SessionInfo, 0, len(b.sessions))
	for _, s := range b.sessions {
		out = append(out, s.info())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ClientID < out[j].ClientID })
	return out
}

// Session returns one session's info and whether it exists.
func (b *Broker) Session(clientID string) (SessionInfo, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	s := b.sessions[clientID]
	if s == nil {
		return SessionInfo{}, false
	}
	return s.info(), true
}

func (s *Session) info() SessionInfo {
	subs := map[string]byte{}
	for f, q := range s.subs {
		subs[f] = q
	}
	inf := append([]uint16(nil), s.inflightOrder...)
	return SessionInfo{
		ClientID:      s.ClientID,
		Durable:       s.Durable,
		Online:        s.conn != nil,
		Subscriptions: subs,
		Pending:       len(s.pending),
		Inflight:      inf,
		NextPacketID:  s.nextPID,
	}
}

// DeleteSession removes a session from the broker. If it is online the
// connection is closed first (no will message is published; this is an
// administrative purge). Returns whether a session existed.
func (b *Broker) DeleteSession(clientID string) bool {
	b.mu.Lock()
	s := b.sessions[clientID]
	if s == nil {
		b.mu.Unlock()
		return false
	}
	conn := s.conn
	s.conn = nil
	delete(b.sessions, clientID)
	b.markDirtyLocked()
	b.mu.Unlock()

	if conn != nil {
		conn.adminDeleted = true
		conn.stop()
		_ = conn.nc.Close()
	}
	return true
}
