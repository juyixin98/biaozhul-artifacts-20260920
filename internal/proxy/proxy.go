// Package proxy 实现本地 DNS 查询代理：接收逻辑查询请求，向 UDP 上游
// 发送单问题 A/AAAA 查询，安全校验响应并维护 TTL 缓存。
//
// 任何传输/协议级错误（超时、截断、ID 不匹配、压缩指针畸形、名称不一致、
// TTL 最高位置 1 等）都向上返回错误且不写缓存；NOERROR/NXDOMAIN 等
// 合法响应按最小应答 TTL 缓存，TTL 为 0 不缓存。
package proxy

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"net"
	"time"

	"dnsproxy/internal/dnscache"
	"dnsproxy/internal/dnsmsg"
)

var (
	// ErrUnsupportedType 仅支持 A/AAAA。
	ErrUnsupportedType = errors.New("proxy: only A/AAAA queries are supported")
	// ErrInvalidName 查询名称非法。
	ErrInvalidName = errors.New("proxy: invalid query name")
)

// ErrorKind 对错误分类，便于 HTTP 层映射状态码。
type ErrorKind int

const (
	KindInvalidInput ErrorKind = iota // 400
	KindUpstream                      // 502
	KindTimeout                       // 504
)

// Error 是代理返回的带分类错误。
type Error struct {
	Kind ErrorKind
	Msg  string
	Err  error
}

func (e *Error) Error() string {
	if e.Err != nil {
		return e.Msg + ": " + e.Err.Error()
	}
	return e.Msg
}

func (e *Error) Unwrap() error { return e.Err }

// Config 是代理配置。
type Config struct {
	UpstreamAddr string
	Timeout      time.Duration
	Now          func() time.Time // 可为 nil
	NewID        func() (uint16, error)
}

// Proxy 是 DNS 查询代理。
type Proxy struct {
	upstream *net.UDPAddr
	timeout  time.Duration
	cache    *dnscache.Cache
	now      func() time.Time
	newID    func() (uint16, error)
}

// New 创建代理并解析上游地址。
func New(cfg Config) (*Proxy, error) {
	addr, err := net.ResolveUDPAddr("udp", cfg.UpstreamAddr)
	if err != nil {
		return nil, err
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 2 * time.Second
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	newID := cfg.NewID
	if newID == nil {
		newID = randomID
	}
	return &Proxy{
		upstream: addr,
		timeout:  timeout,
		cache:    dnscache.New(now),
		now:      now,
		newID:    newID,
	}, nil
}

// Cache 暴露缓存实例（管理接口用）。
func (p *Proxy) Cache() *dnscache.Cache { return p.cache }

// Answer 是一次查询的结果。
type Answer struct {
	Name    string
	QType   uint16
	RCode   byte
	Answers []dnsmsg.Record
	TTL     uint32 // 命中缓存时为剩余 TTL，否则为上游响应的最小 TTL
	Cached  bool
}

// ParseQType 把查询类型字符串映射为 DNS 类型码。
func ParseQType(qtype string) (uint16, error) {
	switch qtype {
	case "A", "a":
		return dnsmsg.TypeA, nil
	case "AAAA", "aaaa":
		return dnsmsg.TypeAAAA, nil
	default:
		return 0, ErrUnsupportedType
	}
}

// Resolve 执行一次查询（先查缓存）。
func (p *Proxy) Resolve(ctx context.Context, name string, qtype uint16) (*Answer, error) {
	if qtype != dnsmsg.TypeA && qtype != dnsmsg.TypeAAAA {
		return nil, &Error{Kind: KindInvalidInput, Msg: "unsupported query type", Err: ErrUnsupportedType}
	}
	canonical := dnsmsg.CanonicalName(name)
	if _, err := dnsmsg.BuildQuery(0, canonical, qtype); err != nil {
		return nil, &Error{Kind: KindInvalidInput, Msg: "invalid query name", Err: err}
	}

	key := dnscache.Key(canonical, qtype)
	if e := p.cache.Get(key); e != nil {
		return &Answer{
			Name:    e.Name,
			QType:   e.QType,
			RCode:   e.RCode,
			Answers: e.Answers,
			TTL:     e.TTL,
			Cached:  true,
		}, nil
	}

	return p.queryUpstream(ctx, canonical, qtype, key)
}

func (p *Proxy) queryUpstream(ctx context.Context, canonical string, qtype uint16, cacheKey string) (*Answer, error) {
	id, err := p.newID()
	if err != nil {
		return nil, &Error{Kind: KindUpstream, Msg: "generate query id", Err: err}
	}
	req, err := dnsmsg.BuildQuery(id, canonical, qtype)
	if err != nil {
		// 名称在 Resolve 中已校验，这里属于内部错误。
		return nil, &Error{Kind: KindInvalidInput, Msg: "build query", Err: err}
	}

	// 每次查询使用独立本地 socket：应答必须来自查询目标之外的 socket
	// 无法匹配，天然隔离过期/无关数据报。
	conn, err := net.DialUDP("udp", nil, p.upstream)
	if err != nil {
		return nil, &Error{Kind: KindUpstream, Msg: "dial upstream", Err: err}
	}
	defer conn.Close()

	// socket deadline 必须使用真实墙钟；注入的 p.now 仅供缓存 TTL 语义。
	deadline := time.Now().Add(p.timeout)
	if dl, ok := ctx.Deadline(); ok && dl.Before(deadline) {
		deadline = dl
	}
	_ = conn.SetDeadline(deadline)

	if _, err := conn.Write(req); err != nil {
		return nil, &Error{Kind: KindUpstream, Msg: "send query", Err: err}
	}

	buf := make([]byte, 4096)
	for {
		if err := ctx.Err(); err != nil {
			return nil, &Error{Kind: KindTimeout, Msg: "context cancelled", Err: err}
		}
		n, err := conn.Read(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				return nil, &Error{Kind: KindTimeout, Msg: "upstream query timed out", Err: err}
			}
			return nil, &Error{Kind: KindUpstream, Msg: "read upstream response", Err: err}
		}

		msg, perr := dnsmsg.ParseMessage(buf[:n])
		if perr != nil {
			// 畸形报文：继续等待同一 socket 上可能的有效重发/迟到合法包，
			// 但在测试/假服务器模型下直接拒绝更可预期——按错误返回。
			return nil, &Error{Kind: KindUpstream, Msg: "malformed upstream response", Err: perr}
		}
		if verr := validateResponse(id, canonical, qtype, msg); verr != nil {
			return nil, verr
		}

		return p.buildAnswer(msg, canonical, qtype, cacheKey), nil
	}
}

// validateResponse 校验上游响应与本次查询严格匹配。
func validateResponse(reqID uint16, qname string, qtype uint16, m *dnsmsg.Message) *Error {
	if !m.QR {
		return &Error{Kind: KindUpstream, Msg: "upstream packet is not a response"}
	}
	if m.OpCode != 0 {
		return &Error{Kind: KindUpstream, Msg: "unexpected opcode in response"}
	}
	if m.TC {
		return &Error{Kind: KindUpstream, Msg: "upstream response truncated (TC bit set); UDP-only proxy cannot follow truncation"}
	}
	if m.ID != reqID {
		return &Error{Kind: KindUpstream, Msg: "response transaction ID does not match query"}
	}
	if len(m.Questions) != 1 {
		return &Error{Kind: KindUpstream, Msg: "response must contain exactly one question"}
	}
	q := m.Questions[0]
	if dnsmsg.CanonicalName(q.Name) != qname {
		return &Error{Kind: KindUpstream, Msg: "response question name does not match query"}
	}
	if q.Type != qtype || (q.Class != 0 && q.Class != dnsmsg.ClassIN) {
		return &Error{Kind: KindUpstream, Msg: "response question type/class does not match query"}
	}
	// 应答记录的 TTL 最高位不允许置 1（RFC 2181 相对值约定；
	// 32 位最高位语义有歧义，按无效响应拒绝）。
	for _, rr := range m.Answers {
		if rr.TTL&0x80000000 != 0 {
			return &Error{Kind: KindUpstream, Msg: "answer TTL has most significant bit set"}
		}
	}
	return nil
}

// buildAnswer 抽取匹配的应答记录、计算最小 TTL、写缓存。
func (p *Proxy) buildAnswer(m *dnsmsg.Message, qname string, qtype uint16, cacheKey string) *Answer {
	answers := make([]dnsmsg.Record, 0, len(m.Answers))
	minTTL := uint32(0)
	first := true
	for _, rr := range m.Answers {
		if rr.Class != dnsmsg.ClassIN {
			continue
		}
		if dnsmsg.CanonicalName(rr.Name) != qname {
			continue
		}
		if rr.Type != qtype {
			continue
		}
		if qtype == dnsmsg.TypeA && len(rr.IP) != 4 {
			continue
		}
		if qtype == dnsmsg.TypeAAAA && len(rr.IP) != 16 {
			continue
		}
		answers = append(answers, rr)
		if first || rr.TTL < minTTL {
			minTTL = rr.TTL
		}
		first = false
	}

	ans := &Answer{
		Name:    qname,
		QType:   qtype,
		RCode:   m.RCode,
		Answers: answers,
		TTL:     minTTL,
	}

	// 仅缓存真正成功且含匹配地址的响应；错误响应（NXDOMAIN/SERVFAIL、
	// 空答案、零 TTL）不缓存。
	if m.RCode == 0 && len(answers) > 0 && minTTL > 0 {
		p.cache.Set(cacheKey, &dnscache.Entry{
			Name:    qname,
			QType:   qtype,
			Answers: answers,
			RCode:   m.RCode,
			TTL:     minTTL,
		}, minTTL)
	}
	return ans
}

func randomID() (uint16, error) {
	var b [2]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0, err
	}
	return binary.BigEndian.Uint16(b[:]), nil
}
