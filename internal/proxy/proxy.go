// Package proxy performs UDP DNS lookups against one upstream server and caches
// usable answers by their RR TTL.
//
// It only ever sends queries with a single IN-class question of type A or
// AAAA. Responses must satisfy every check below before they touch the cache:
//
//   - received on UDP within the timeout and at least 12 bytes long
//   - transaction ID matches the query we sent
//   - QR=1, TC=0, RCODE=0
//   - exactly one question matching our name and type, class IN
//   - at least one A/AAAA answer, class IN, correct rdata length, owner name
//     equal to the question name (CNAME chains are not supported and rejected)
//
// Any failure produces an error and is not cached.
package proxy

import (
	"errors"
	"fmt"
	"net"
	"strings"
	"time"

	"dnscomp-proxy/internal/cache"
	"dnscomp-proxy/internal/dnsmsg"
)

// Sentinel error classes. The HTTP layer maps these to status codes.
var (
	ErrInvalidRequest = errors.New("invalid request")
	ErrUpstream       = errors.New("upstream error")
	ErrBadResponse    = errors.New("malformed upstream response")
)

// Config configures a Proxy.
type Config struct {
	UpstreamAddr string        // host:port of the UDP DNS upstream (local fake server)
	Timeout      time.Duration // per-query UDP read/write deadline
	MaxUDPSize   int           // maximum response size accepted (default 512)
}

// Proxy is the DNS lookup proxy.
type Proxy struct {
	cfg    Config
	cache  *cache.Cache
	dialer func() (net.Conn, error)
}

// New builds a Proxy over a standard UDP dialer.
func New(cfg Config, c *cache.Cache) (*Proxy, error) {
	if _, _, err := net.SplitHostPort(cfg.UpstreamAddr); err != nil {
		return nil, fmt.Errorf("bad upstream address: %w", err)
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 2 * time.Second
	}
	if cfg.MaxUDPSize <= 0 {
		cfg.MaxUDPSize = 512
	}
	p := &Proxy{cfg: cfg, cache: c}
	p.dialer = p.defaultDial
	return p, nil
}

func (p *Proxy) defaultDial() (net.Conn, error) {
	conn, err := net.Dial("udp", p.cfg.UpstreamAddr)
	if err != nil {
		return nil, err
	}
	return conn, nil
}

// Result is a successful resolution returned to HTTP clients.
type Result struct {
	Name    string          `json:"name"`
	Type    string          `json:"type"`
	Answers []cache.Address `json:"answers"`
	Source  string          `json:"source"` // "cache" or "upstream"
}

// Resolve performs one A/AAAA resolution, using the cache when live.
func (p *Proxy) Resolve(qname string, qtype uint16) (*Result, error) {
	if qtype != dnsmsg.TypeA && qtype != dnsmsg.TypeAAAA {
		return nil, fmt.Errorf("%w: qtype must be A(1) or AAAA(28)", ErrInvalidRequest)
	}
	canon := dnsmsg.CanonicalName(qname)
	key := cache.Key{Name: canon, QType: qtype}

	if e, ok := p.cache.Get(key); ok {
		return &Result{Name: canon, Type: typeName(qtype), Answers: e.Answers, Source: "cache"}, nil
	}

	id, err := dnsmsg.NewID()
	if err != nil {
		return nil, fmt.Errorf("allocating query id: %w", err)
	}
	query, err := dnsmsg.BuildQuery(id, canon, qtype)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}

	msg, err := p.exchange(query)
	if err != nil {
		return nil, err
	}

	answers, minTTL, err := validateResponse(msg, id, canon, qtype)
	if err != nil {
		// Defensive: explicit non-caching. validateResponse only returns
		// data for fully valid answers; nothing below runs on error.
		return nil, err
	}

	entry := cache.Entry{Answers: answers}
	ttl := time.Duration(minTTL) * time.Second
	// Set itself refuses ttl<=0 (TTL 0 boundary): such answers are returned
	// to this caller but never stored.
	p.cache.Set(key, entry, ttl)

	// Report the source honestly if Set refused it.
	source := "upstream"
	if minTTL == 0 {
		source = "upstream (ttl=0, not cached)"
	}
	return &Result{Name: canon, Type: typeName(qtype), Answers: answers, Source: source}, nil
}

// exchange sends one UDP query and reads one datagram with a deadline.
func (p *Proxy) exchange(query []byte) ([]byte, error) {
	conn, err := p.dialer()
	if err != nil {
		return nil, fmt.Errorf("%w: dial: %v", ErrUpstream, err)
	}
	defer conn.Close()

	deadline := time.Now().Add(p.cfg.Timeout)
	if err := conn.SetDeadline(deadline); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUpstream, err)
	}
	if _, err := conn.Write(query); err != nil {
		return nil, fmt.Errorf("%w: write: %v", ErrUpstream, err)
	}
	buf := make([]byte, p.cfg.MaxUDPSize)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("%w: read: %v", ErrUpstream, err)
	}
	return buf[:n], nil
}

// validateResponse enforces every response rule. It returns the accepted
// answer list and the minimum TTL among them (the cache lifetime).
func validateResponse(raw []byte, wantID uint16, wantName string, wantType uint16) ([]cache.Address, uint32, error) {
	m, _, err := dnsmsg.ParseMessage(raw)
	if err != nil {
		return nil, 0, fmt.Errorf("%w: %v", ErrBadResponse, err)
	}

	if m.ID != wantID {
		return nil, 0, fmt.Errorf("%w: transaction id mismatch: got %d want %d", ErrBadResponse, m.ID, wantID)
	}
	if !m.QR {
		return nil, 0, fmt.Errorf("%w: QR bit not set", ErrBadResponse)
	}
	if m.TC {
		return nil, 0, fmt.Errorf("%w: response has TC (truncation) set; TCP fallback not supported", ErrBadResponse)
	}
	if m.RCode != dnsmsg.RCodeOK {
		return nil, 0, fmt.Errorf("%w: upstream rcode=%d (%s)", ErrBadResponse, m.RCode, rcodeName(m.RCode))
	}
	if len(m.Questions) != 1 {
		return nil, 0, fmt.Errorf("%w: expected 1 question, got %d", ErrBadResponse, len(m.Questions))
	}
	q := m.Questions[0]
	if !strings.EqualFold(q.Name, wantName) || q.Type != wantType || q.Class != dnsmsg.ClassIN {
		return nil, 0, fmt.Errorf("%w: question echo mismatch (%s/%d/%d vs %s/%d/IN)",
			ErrBadResponse, q.Name, q.Type, q.Class, wantName, wantType)
	}
	if len(m.Answers) == 0 {
		return nil, 0, fmt.Errorf("%w: rcode=0 but answer section is empty", ErrBadResponse)
	}

	out := make([]cache.Address, 0, len(m.Answers))
	var minTTL uint32 = ^uint32(0)
	for i, rr := range m.Answers {
		if rr.Type != wantType {
			return nil, 0, fmt.Errorf("%w: answer %d has unexpected type %d (CNAME/other not supported)", ErrBadResponse, i, rr.Type)
		}
		if rr.Class != dnsmsg.ClassIN {
			return nil, 0, fmt.Errorf("%w: answer %d class=%d not IN", ErrBadResponse, i, rr.Class)
		}
		if !strings.EqualFold(rr.Name, wantName) {
			return nil, 0, fmt.Errorf("%w: answer %d owner %q != qname %q", ErrBadResponse, i, rr.Name, wantName)
		}
		wantLen := 4
		if wantType == dnsmsg.TypeAAAA {
			wantLen = 16
		}
		if len(rr.IP) != wantLen {
			return nil, 0, fmt.Errorf("%w: answer %d rdata length %d, want %d", ErrBadResponse, i, len(rr.IP), wantLen)
		}
		out = append(out, cache.Address{
			Type: typeName(wantType),
			IP:   net.IP(rr.IP).String(),
			TTL:  rr.TTL,
		})
		if rr.TTL < minTTL {
			minTTL = rr.TTL
		}
	}
	return out, minTTL, nil
}

func typeName(t uint16) string {
	switch t {
	case dnsmsg.TypeA:
		return "A"
	case dnsmsg.TypeAAAA:
		return "AAAA"
	default:
		return fmt.Sprintf("%d", t)
	}
}

func rcodeName(code uint8) string {
	switch code {
	case 0:
		return "NOERROR"
	case 1:
		return "FORMERR"
	case 2:
		return "SERVFAIL"
	case 3:
		return "NXDOMAIN"
	case 4:
		return "NOTIMP"
	case 5:
		return "REFUSED"
	default:
		return "UNKNOWN"
	}
}

// Cache exposes the cache to the HTTP layer.
func (p *Proxy) Cache() *cache.Cache { return p.cache }

// SetDialer replaces the UDP dialer. Used by tests to script responses.
func (p *Proxy) SetDialer(d func() (net.Conn, error)) { p.dialer = d }
