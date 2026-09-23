// Package fakeserver is a locally controlled UDP DNS upstream used for tests
// and demos. It never touches the network beyond its bound UDP socket.
//
// Besides ordinary A/AAAA answers from an in-memory zone, it serves a fixed
// set of crafted responses keyed by queried name. These exercise the proxy's
// hardening without needing to hand-craft packets over the HTTP boundary:
//
//	loop.example.      answer name is a two-node compression-pointer ring
//	fwd.example.       compression pointer target past the end of the message
//	chain.example.     128-jump backward pointer chain (loop/cap defense)
//	trunc.example.     well-formed header, RDLENGTH overruns the packet
//	short.example.     6-byte datagram (shorter than the DNS header)
//	badid.example.     valid answer whose transaction ID is flipped
//	nxdomain.example.  RCODE=3, no answers
//	noans.example.     RCODE=0 but empty answer section
//	tc.example.        TC (truncation) bit set
//	tql0.example.      valid A answer with TTL 0
//	ttlmax.example.    two A answers with TTL 4294967295 and TTL 5
//	multiq.example.    response carries two questions
package fakeserver

import (
	"encoding/binary"
	"net"
	"strings"
	"sync"
	"sync/atomic"

	"dnscomp-proxy/internal/dnsmsg"
)

// RecordConfig configures one zone record.
type RecordConfig struct {
	Name string // "example.com" or trailing dot, case-insensitive
	Type uint16 // dnsmsg.TypeA / TypeAAAA
	IP   net.IP
	TTL  uint32
}

// Server is a fake UDP DNS server.
type Server struct {
	conn    *net.UDPConn
	mu      sync.RWMutex
	records map[string]map[uint16][]RecordConfig
	hits    sync.Map // string canonical name -> *int64
}

// New binds a UDP server on addr ("127.0.0.1:0" for an ephemeral port) and
// loads the given records.
func New(addr string, records []RecordConfig) (*Server, error) {
	udpAddr, err := net.ResolveUDPAddr("udp", addr)
	if err != nil {
		return nil, err
	}
	conn, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return nil, err
	}
	s := &Server{
		conn:    conn,
		records: make(map[string]map[uint16][]RecordConfig),
	}
	for _, r := range records {
		s.AddRecord(r)
	}
	return s, nil
}

// AddRecord adds one zone record at runtime.
func (s *Server) AddRecord(r RecordConfig) {
	name := dnsmsg.CanonicalName(r.Name)
	s.mu.Lock()
	defer s.mu.Unlock()
	byType, ok := s.records[name]
	if !ok {
		byType = make(map[uint16][]RecordConfig)
		s.records[name] = byType
	}
	r.Name = name
	byType[r.Type] = append(byType[r.Type], r)
}

// LocalAddr is the bound address (useful with port 0).
func (s *Server) LocalAddr() *net.UDPAddr { return s.conn.LocalAddr().(*net.UDPAddr) }

// Hits reports how many upstream queries this name received (cache validation).
func (s *Server) Hits(name string) int64 {
	v, ok := s.hits.Load(dnsmsg.CanonicalName(name))
	if !ok {
		return 0
	}
	return atomic.LoadInt64(v.(*int64))
}

// Close stops the server.
func (s *Server) Close() error { return s.conn.Close() }

// Serve handles packets until the socket is closed.
func (s *Server) Serve() error {
	buf := make([]byte, 1500)
	for {
		n, raddr, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			if isClosedErr(err) {
				return nil
			}
			return err
		}
		resp := s.handle(append([]byte(nil), buf[:n]...))
		if resp == nil {
			continue
		}
		if _, err := s.conn.WriteToUDP(resp, raddr); err != nil && !isClosedErr(err) {
			return err
		}
	}
}

func isClosedErr(err error) bool {
	return strings.Contains(err.Error(), "closed")
}

func (s *Server) handle(req []byte) []byte {
	m, _, err := dnsmsg.ParseMessage(req)
	if err != nil || len(m.Questions) == 0 {
		return nil // drop garbage, like a dead upstream
	}
	q := m.Questions[0]

	if v, ok := s.hits.Load(q.Name); ok {
		atomic.AddInt64(v.(*int64), 1)
	} else {
		var c int64 = 1
		s.hits.Store(q.Name, &c)
	}

	// Hostile / boundary crafted responses take priority.
	if b := craftSpecial(m.ID, q); b != nil {
		return b
	}

	s.mu.RLock()
	byType, ok := s.records[q.Name]
	var recs []RecordConfig
	if ok {
		recs = byType[q.Type]
	}
	s.mu.RUnlock()
	if !ok || len(recs) == 0 {
		return craftNXDOMAIN(m.ID, q)
	}
	return craftAnswer(m.ID, q, recs)
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

func baseHeader(id uint16, flags uint16, qd, an, ns, ar int) []byte {
	h := make([]byte, dnsmsg.HeaderLen)
	binary.BigEndian.PutUint16(h[0:2], id)
	binary.BigEndian.PutUint16(h[2:4], flags)
	binary.BigEndian.PutUint16(h[4:6], uint16(qd))
	binary.BigEndian.PutUint16(h[6:8], uint16(an))
	binary.BigEndian.PutUint16(h[8:10], uint16(ns))
	binary.BigEndian.PutUint16(h[10:12], uint16(ar))
	return h
}

func appendQuestion(b []byte, q dnsmsg.Question) []byte {
	enc, err := dnsmsg.EncodeName(q.Name)
	if err != nil {
		enc = []byte{0}
	}
	b = append(b, enc...)
	b = appendU16(b, q.Type)
	b = appendU16(b, q.Class)
	return b
}

func appendU16(b []byte, v uint16) []byte {
	return append(b, byte(v>>8), byte(v))
}

func appendU32(b []byte, v uint32) []byte {
	return append(b, byte(v>>24), byte(v>>16), byte(v>>8), byte(v))
}

const flagsQR = uint16(0x8180) // QR + RD + RA + RCODE0
const flagsTC = uint16(0x8380) // QR + RD + RA + TC
const flagsNX = uint16(0x8183) // QR + RD + RA + RCODE3

func craftAnswer(id uint16, q dnsmsg.Question, recs []RecordConfig) []byte {
	b := baseHeader(id, flagsQR, 1, len(recs), 0, 0)
	b = appendQuestion(b, q)
	for _, r := range recs {
		b = appendU16(b, 0xc00c) // name pointer to question name at offset 12
		b = appendU16(b, r.Type)
		b = appendU16(b, dnsmsg.ClassIN)
		b = appendU32(b, r.TTL)
		b = appendU16(b, uint16(len(r.IP)))
		b = append(b, r.IP...)
	}
	return b
}

func craftNXDOMAIN(id uint16, q dnsmsg.Question) []byte {
	b := baseHeader(id, flagsNX, 1, 0, 0, 0)
	b = appendQuestion(b, q)
	return b
}

// craftSpecial returns a crafted response for the built-in hostile names, or
// nil if the name is a normal zone lookup.
func craftSpecial(id uint16, q dnsmsg.Question) []byte {
	switch q.Name {
	case "loop.example":
		// Two-node ring: answer name at offset N points to N+2, where a
		// second pointer points back to N. A cycle requires a forward edge;
		// the parser rejects that edge (ErrForwardPointer), breaking the ring.
		b := baseHeader(id, flagsQR, 1, 1, 0, 0)
		b = appendQuestion(b, q)
		ringStart := len(b)
		b = appendU16(b, 0xc000|uint16(ringStart+2)) // -> ringStart+2
		b = appendU16(b, 0xc000|uint16(ringStart))   // -> ringStart
		return b

	case "fwd.example":
		// Answer name points to offset 64, past the end of the message.
		b := baseHeader(id, flagsQR, 1, 1, 0, 0)
		b = appendQuestion(b, q)
		b = appendU16(b, 0xc040)
		return b

	case "chain.example":
		return craftLongChain(id, q)

	case "trunc.example":
		// One answer claims 255 bytes of rdata but only 4 follow.
		b := baseHeader(id, flagsQR, 1, 1, 0, 0)
		b = appendQuestion(b, q)
		b = appendU16(b, 0xc00c)
		b = appendU16(b, dnsmsg.TypeA)
		b = appendU16(b, dnsmsg.ClassIN)
		b = appendU32(b, 60)
		b = appendU16(b, 255)
		b = append(b, 192, 0, 2, 9)
		return b

	case "short.example":
		return []byte{0, byte(id & 0xff), 0, 0, 0, 0}

	case "badid.example":
		// Well-formed answer carrying the wrong transaction ID.
		recs := []RecordConfig{{Name: q.Name, Type: q.Type, IP: net.IPv4(192, 0, 2, 19).To4(), TTL: 30}}
		b := craftAnswer(id^0xffff, q, recs)
		return b

	case "nxdomain.example":
		return craftNXDOMAIN(id, q)

	case "noans.example":
		b := baseHeader(id, flagsQR, 1, 0, 0, 0)
		b = appendQuestion(b, q)
		return b

	case "tc.example":
		b := baseHeader(id, flagsTC, 1, 0, 0, 0)
		b = appendQuestion(b, q)
		return b

	case "tql0.example":
		recs := []RecordConfig{{Name: q.Name, Type: q.Type, IP: net.IPv4(192, 0, 2, 10).To4(), TTL: 0}}
		return craftAnswer(id, q, recs)

	case "ttlmax.example":
		recs := []RecordConfig{
			{Name: q.Name, Type: q.Type, IP: net.IPv4(192, 0, 2, 31).To4(), TTL: 4294967295},
			{Name: q.Name, Type: q.Type, IP: net.IPv4(192, 0, 2, 32).To4(), TTL: 5},
		}
		return craftAnswer(id, q, recs)

	case "multiq.example":
		// Response with two questions; proxy requires exactly one.
		b := baseHeader(id, flagsQR, 2, 0, 0, 0)
		b = appendQuestion(b, q)
		b = appendQuestion(b, dnsmsg.Question{Name: "extra.example", Type: dnsmsg.TypeA, Class: dnsmsg.ClassIN})
		return b
	}
	return nil
}

// craftLongChain builds a response whose second additional-RR name is the
// entry pointer of a 128-jump strictly-backward compression-pointer chain.
// The 127 chain nodes are hidden in the first additional RR's RDATA, which
// the parser only consumes as opaque bytes (it never name-decodes rdata), so
// the only way to reach them is the second RR's owner-name pointer. Every
// edge points backwards and inside the message (legal by the backward rule),
// so only the parser's jump cap / visited-set defense can stop it:
// ParseMessage must return ErrCompressionLoop.
//
// Layout:
//
//	header (QD=1, AR=2)
//	question
//	additional RR 1: root owner, type A, rdata =
//	    [root 00][pad][127 pointer nodes, each 2 bytes further back]
//	additional RR 2: owner name = pointer to the top node (128th jump when
//	    traversed), followed by inert fixed-field bytes
func craftLongChain(id uint16, q dnsmsg.Question) []byte {
	b := baseHeader(id, flagsQR, 1, 0, 0, 2)
	b = appendQuestion(b, q)

	// Additional RR 1: root owner and fixed fields.
	b = append(b, 0x00)
	b = appendU16(b, dnsmsg.TypeA)
	b = appendU16(b, dnsmsg.ClassIN)
	b = appendU32(b, 60)

	rdataLenOff := len(b)
	b = appendU16(b, 0) // placeholder, patched after rdata is built
	rdataStart := len(b)

	b = append(b, 0x00) // terminator a legal chain would eventually reach
	if len(b)%2 != 0 {
		b = append(b, 0)
	}
	rootPos := rdataStart

	// 127 chain nodes; together with the RR-2 entry pointer that is 128
	// backward jumps, tripping the parser's cap.
	const nodeCount = 127
	firstNode := len(b)
	for i := 0; i < nodeCount; i++ {
		target := rootPos
		if i > 0 {
			target = firstNode + 2*(i-1)
		}
		b = appendU16(b, 0xc000|uint16(target))
	}
	topNode := firstNode + 2*(nodeCount-1)

	binary.BigEndian.PutUint16(b[rdataLenOff:rdataLenOff+2], uint16(len(b)-rdataStart))

	// Additional RR 2: owner name jumps to the top node (the 128th jump).
	b = appendU16(b, 0xc000|uint16(topNode))
	// Fixed fields after the name are never reached on the attack path.
	b = append(b, 0, 1, 0, 1, 0, 0, 0, 60, 0, 0)
	return b
}
