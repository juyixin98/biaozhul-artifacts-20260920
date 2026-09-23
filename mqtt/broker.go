package mqtt

import (
	"log"
	"net"
	"sync"
)

// inflight is one outbound QoS 1 message awaiting PUBACK.
type inflight struct {
	pid uint16
	msg message
}

// message is an MQTT application message in flight through the broker.
type message struct {
	topic   string
	qos     byte // QoS the PUBLISH was published with (0 or 1)
	payload []byte
}

// session is the per-Client-ID MQTT session state (§3.1.2). Sessions with
// clean=false survive disconnects and broker restarts; clean sessions live
// only for the current connection and are never persisted.
type session struct {
	clientID string
	clean    bool

	subs map[string]byte // filter -> maximum QoS granted

	// Outbound (broker -> client) delivery state.
	inflight map[uint16]*inflight
	queue    []message // offline QoS 1 messages awaiting delivery
	order    []uint16  // stable send order of inflight PIDs
	nextPID  uint16

	conn *conn // nil while the client is offline
}

func newSession(clientID string, clean bool) *session {
	return &session{
		clientID: clientID,
		clean:    clean,
		subs:     map[string]byte{},
		inflight: map[uint16]*inflight{},
		nextPID:  1,
	}
}

// allocPID returns the next unused outbound packet identifier for this
// session. Packet IDs are managed per session and per direction (this is the
// server -> client direction); 65535 in flight at once is the hard limit.
func (s *session) allocPID() (uint16, bool) {
	for range 65536 {
		pid := s.nextPID
		s.nextPID++
		if s.nextPID == 0 {
			s.nextPID = 1
		}
		if pid == 0 {
			continue
		}
		if _, used := s.inflight[pid]; used {
			continue
		}
		return pid, true
	}
	return 0, false
}

// Broker is the MQTT 3.1.1 subset broker.
type Broker struct {
	mu       sync.Mutex
	sessions map[string]*session
	store    *store
	logger   *log.Logger

	listener net.Listener
	wg       sync.WaitGroup
	closed   bool

	// OnPublish, when non-nil, is invoked after an inbound PUBLISH has been
	// accepted (post fan-out), purely for observability/tests.
	OnPublish func(clientID, topic string, qos byte, payload []byte)
}

// NewBroker creates a broker backed by JSON state in stateDir. If stateDir
// contains a snapshot from a previous run, persistent sessions are resumed
// transparently on the next CONNECT with clean=false.
func NewBroker(stateDir string, logger *log.Logger) (*Broker, error) {
	st, err := newStore(stateDir)
	if err != nil {
		return nil, err
	}
	snap, err := st.load()
	if err != nil {
		return nil, err
	}
	if logger == nil {
		logger = log.New(log.Writer(), "[mqtt] ", log.LstdFlags)
	}
	b := &Broker{
		sessions: map[string]*session{},
		store:    st,
		logger:   logger,
	}
	for id, ss := range snap.Sessions {
		b.sessions[id] = sessionFromStored(id, ss)
	}
	return b, nil
}

func sessionFromStored(id string, ss storedSession) *session {
	s := newSession(id, false)
	s.subs = ss.Subscriptions
	if s.subs == nil {
		s.subs = map[string]byte{}
	}
	s.nextPID = ss.NextPacketID
	if s.nextPID == 0 {
		s.nextPID = 1
	}
	for _, im := range ss.Inflight {
		s.inflight[im.PacketID] = &inflight{
			pid: im.PacketID,
			msg: message{
				topic:   im.Message.Topic,
				qos:     im.Message.QoS,
				payload: im.Message.Payload,
			},
		}
		s.order = append(s.order, im.PacketID)
	}
	for _, q := range ss.Queue {
		s.queue = append(s.queue, message{topic: q.Topic, qos: q.QoS, payload: q.Payload})
	}
	return s
}

// snapshotLocked builds the persistence view of all clean=false sessions.
// Caller must hold b.mu.
func (b *Broker) snapshotLocked() *snapshot {
	snap := &snapshot{Sessions: map[string]storedSession{}}
	for id, s := range b.sessions {
		if s.clean {
			continue
		}
		ss := storedSession{
			Subscriptions: map[string]byte{},
			NextPacketID:  s.nextPID,
		}
		for f, q := range s.subs {
			ss.Subscriptions[f] = q
		}
		for _, pid := range s.order {
			if in, ok := s.inflight[pid]; ok {
				ss.Inflight = append(ss.Inflight, storedInflight{
					PacketID: pid,
					Message:  storedMessage{Topic: in.msg.topic, QoS: in.msg.qos, Payload: in.msg.payload},
				})
			}
		}
		for _, q := range s.queue {
			ss.Queue = append(ss.Queue, storedMessage{Topic: q.topic, QoS: q.qos, Payload: q.payload})
		}
		snap.Sessions[id] = ss
	}
	return snap
}

// persistLocked flushes durable state. Caller must hold b.mu; errors are
// logged but do not crash the broker (the in-memory state stays authoritative
// for the running process).
func (b *Broker) persistLocked() {
	if err := b.store.save(b.snapshotLocked()); err != nil {
		b.logger.Printf("persist error: %v", err)
	}
}

// SessionInfo is a read-only view of session state for the HTTP API.
type SessionInfo struct {
	ClientID      string         `json:"client_id"`
	CleanSession  bool           `json:"clean_session"`
	Online        bool           `json:"online"`
	Subscriptions map[string]int `json:"subscriptions"`
	InflightCount int            `json:"inflight_count"`
	QueuedCount   int            `json:"queued_count"`
	NextPacketID  uint16         `json:"next_packet_id"`
}

// Sessions returns current session views.
func (b *Broker) Sessions() []SessionInfo {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]SessionInfo, 0, len(b.sessions))
	for _, s := range b.sessions {
		info := SessionInfo{
			ClientID:      s.clientID,
			CleanSession:  s.clean,
			Online:        s.conn != nil,
			Subscriptions: map[string]int{},
			InflightCount: len(s.inflight),
			QueuedCount:   len(s.queue),
			NextPacketID:  s.nextPID,
		}
		for f, q := range s.subs {
			info.Subscriptions[f] = int(q)
		}
		out = append(out, info)
	}
	return out
}

// DeleteSession removes a session and its durable state, disconnecting any
// live transport. Used by the HTTP API (and tests). It returns false if no
// such session exists.
func (b *Broker) DeleteSession(clientID string) bool {
	b.mu.Lock()
	s, ok := b.sessions[clientID]
	if !ok {
		b.mu.Unlock()
		return false
	}
	c := s.conn
	delete(b.sessions, clientID)
	b.persistLocked()
	b.mu.Unlock()
	if c != nil {
		c.close()
	}
	return true
}

// Publish injects an application message from outside MQTT (HTTP API). It
// applies the same delivery and persistence rules as an inbound PUBLISH and
// returns the number of currently-matching sessions (online or queued).
func (b *Broker) Publish(topic string, qos byte, payload []byte) (int, error) {
	if !validPublishTopic(topic) {
		return 0, ErrProtocol
	}
	if qos > 1 {
		return 0, ErrProtocol
	}
	b.mu.Lock()
	writes, n := b.planDeliveryLocked(message{topic: topic, qos: qos, payload: payload})
	b.persistLocked()
	b.mu.Unlock()
	b.flushWrites(writes)
	return n, nil
}

// plannedWrite is one encoded packet that must be sent after the broker lock
// has been released; the corresponding delivery state is already durable.
type plannedWrite struct {
	s   *session
	c   *conn
	pkt []byte
}

// planDeliveryLocked fans one message out across sessions at the state level.
// It updates subscription delivery state:
//   - online QoS 1 -> a packet ID is allocated and the message is recorded as
//     inflight (survives a crash before the bytes leave the process, which
//     only ever causes a DUP=1 redelivery, never a loss);
//   - offline QoS 1 -> queued for the next connection;
//   - QoS 0 (online, or downgraded by the granted QoS) -> fire-and-forget,
//     never tracked or queued.
//
// The returned writes must be performed WITHOUT the lock. matched counts all
// sessions whose subscriptions matched, whether online or queued.
// Caller holds b.mu.
func (b *Broker) planDeliveryLocked(m message) (writes []plannedWrite, matched int) {
	for _, s := range b.sessions {
		grant, ok := bestGrant(s.subs, m.topic)
		if !ok {
			continue
		}
		matched++
		effQoS := m.qos
		if grant < effQoS {
			effQoS = grant
		}
		out := message{topic: m.topic, qos: effQoS, payload: m.payload}
		switch {
		case effQoS == 0 && s.conn != nil:
			writes = append(writes, plannedWrite{
				s: s, c: s.conn,
				pkt: publishPacket(false, 0, false, out.topic, 0, out.payload),
			})
		case effQoS >= 1 && s.conn != nil:
			pid, ok := s.allocPID()
			if !ok {
				// Packet-ID space exhausted: hold the message durably until
				// PUBACKs free identifiers.
				enqueue(s, out)
				continue
			}
			s.inflight[pid] = &inflight{pid: pid, msg: out}
			s.order = append(s.order, pid)
			writes = append(writes, plannedWrite{
				s: s, c: s.conn,
				pkt: publishPacket(false, 1, false, out.topic, pid, out.payload),
			})
		case effQoS >= 1:
			enqueue(s, out)
		}
		// QoS 0 to an offline session is dropped per §3.1.2-5's queueing rules
		// (only QoS 1/2 messages are queued for an offline client).
	}
	return writes, matched
}

// flushWrites sends planned packets without holding the broker lock. A failed
// write marks that session offline (its inflight messages stay tracked and are
// redelivered with DUP=1 on reconnect), preserving at-least-once delivery.
func (b *Broker) flushWrites(writes []plannedWrite) {
	for _, w := range writes {
		if !w.c.writePacket(w.pkt) {
			b.markOffline(w.s, w.c)
		}
	}
}

// markOffline detaches c from its session when it is still the current
// transport, then closes it. Safe to call after a takeover (in which case the
// newer transport is left untouched).
func (b *Broker) markOffline(s *session, c *conn) {
	b.mu.Lock()
	if cur, ok := b.sessions[s.clientID]; ok && cur == s && s.conn == c {
		s.conn = nil
		b.persistLocked()
	}
	b.mu.Unlock()
	c.close()
}

func enqueue(s *session, m message) {
	// Queue cap is deliberately bounded to keep the state file sane.
	const maxQueue = 10000
	if len(s.queue) >= maxQueue {
		s.queue = s.queue[1:]
	}
	s.queue = append(s.queue, m)
}

// bestGrant returns the maximum granted QoS across all matching filters
// (MQTT delivers from the matching subscription with the highest QoS).
func bestGrant(subs map[string]byte, topic string) (byte, bool) {
	best := byte(0)
	found := false
	for f, q := range subs {
		if topicMatch(f, topic) {
			if !found || q > best {
				best = q
			}
			found = true
		}
	}
	return best, found
}
