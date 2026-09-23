package proxy_test

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"dnsproxy/internal/dnsmsg"
	"dnsproxy/internal/fakedns"
	"dnsproxy/internal/proxy"
)

// testEnv 捆绑一个代理与可替换 handler 的假上游。
type testEnv struct {
	t         *testing.T
	srv       *fakedns.Server
	clk       *fakeClock
	idSeq     *uint32
	handlerMu sync.RWMutex
	handler   func(fakedns.Request) []byte
}

func (e *testEnv) setHandler(h func(fakedns.Request) []byte) {
	e.handlerMu.Lock()
	e.handler = h
	e.handlerMu.Unlock()
}

func (e *testEnv) currentHandler() func(fakedns.Request) []byte {
	e.handlerMu.RLock()
	h := e.handler
	e.handlerMu.RUnlock()
	return h
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time          { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newEnv(t *testing.T) (*proxy.Proxy, *testEnv) {
	t.Helper()
	env := &testEnv{
		t:   t,
		clk: &fakeClock{t: time.Unix(1_000_000, 0)},
	}
	srv, err := fakedns.Start("127.0.0.1:0", func(req fakedns.Request) []byte {
		h := env.currentHandler()
		if h == nil {
			return nil
		}
		return h(req)
	})
	if err != nil {
		t.Fatalf("start fakedns: %v", err)
	}
	t.Cleanup(func() { _ = srv.Close() })
	env.srv = srv

	var id uint32
	env.idSeq = &id
	p, err := proxy.New(proxy.Config{
		UpstreamAddr: srv.Addr(),
		Timeout:      200 * time.Millisecond,
		Now:          env.clk.now,
		NewID: func() (uint16, error) {
			return uint16(atomic.AddUint32(&id, 1)), nil
		},
	})
	if err != nil {
		t.Fatalf("new proxy: %v", err)
	}
	return p, env
}

// goodResponse 是一个标准合法应答（压缩指针名）。
func goodResponse(m *dnsmsg.Message) []byte {
	q := m.Questions[0]
	ip := net.ParseIP("192.0.2.10")
	if q.Type == dnsmsg.TypeAAAA {
		ip = net.ParseIP("2001:db8::10")
	}
	resp, err := dnsmsg.BuildResponse(m.ID, q, 0, false, []dnsmsg.AnswerSpec{
		{Name: q.Name, TTL: 60, IP: ip},
	})
	if err != nil {
		panic(err)
	}
	return resp
}

func ptrResponse(m *dnsmsg.Message, ttl uint32, ansNamePtr uint16) []byte {
	q := m.Questions[0]
	qname, _ := dnsmsg.BuildQuery(m.ID, q.Name, q.Type)
	out := make([]byte, 0, 64)

	hdr := make([]byte, 12)
	binary.BigEndian.PutUint16(hdr[0:2], m.ID)
	binary.BigEndian.PutUint16(hdr[2:4], 0x8180)
	binary.BigEndian.PutUint16(hdr[4:6], 1)
	binary.BigEndian.PutUint16(hdr[6:8], 1)
	out = append(out, hdr...)

	// 问题段直接复用 BuildQuery 的问题部分。
	out = append(out, qname[12:]...)
	// answer name = 给定压缩指针
	ptr := make([]byte, 2)
	binary.BigEndian.PutUint16(ptr, ansNamePtr)
	out = append(out, ptr...)
	fixed := make([]byte, 8)
	binary.BigEndian.PutUint16(fixed[0:2], dnsmsg.TypeA)
	binary.BigEndian.PutUint16(fixed[2:4], dnsmsg.ClassIN)
	binary.BigEndian.PutUint32(fixed[4:8], ttl)
	out = append(out, fixed...)
	rl := make([]byte, 2)
	binary.BigEndian.PutUint16(rl, 4)
	out = append(out, rl...)
	out = append(out, 192, 0, 2, 10)
	return out
}

func TestResolveHappyPathAndCacheHit(t *testing.T) {
	p, env := newEnv(t)
	var upstreamCalls int32
	env.setHandler(func(req fakedns.Request) []byte {
		atomic.AddInt32(&upstreamCalls, 1)
		return goodResponse(req.Message)
	})

	ans, err := p.Resolve(context.Background(), "example.com", dnsmsg.TypeA)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if ans.Cached || len(ans.Answers) != 1 || ans.Answers[0].IP.String() != "192.0.2.10" || ans.TTL != 60 {
		t.Fatalf("unexpected answer: %+v", ans)
	}

	// 第二次：命中缓存，上游不再被查询。
	ans2, err := p.Resolve(context.Background(), "EXAMPLE.COM.", dnsmsg.TypeA)
	if err != nil {
		t.Fatalf("Resolve cached: %v", err)
	}
	if !ans2.Cached || ans2.TTL != 60 {
		t.Fatalf("expected cache hit, got %+v", ans2)
	}
	if atomic.LoadInt32(&upstreamCalls) != 1 {
		t.Fatalf("upstream called %d times, want 1", upstreamCalls)
	}
}

func TestCacheExpiresPerTTL(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		return goodResponse(req.Message)
	})

	if _, err := p.Resolve(context.Background(), "example.com", dnsmsg.TypeA); err != nil {
		t.Fatal(err)
	}
	env.clk.advance(59 * time.Second)
	if _, err := p.Resolve(context.Background(), "example.com", dnsmsg.TypeA); err != nil {
		t.Fatal(err)
	}
	if env.srv.RequestsReceived() != 1 {
		t.Fatalf("want 1 upstream call before expiry, got %d", env.srv.RequestsReceived())
	}

	env.clk.advance(2 * time.Second) // 越过 60s TTL
	if _, err := p.Resolve(context.Background(), "example.com", dnsmsg.TypeA); err != nil {
		t.Fatal(err)
	}
	if env.srv.RequestsReceived() != 2 {
		t.Fatalf("want re-query after expiry, got %d calls", env.srv.RequestsReceived())
	}
}

// TTL=0：成功应答也不得入缓存，每次都打上游。
func TestTTLZeroNotCached(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		return ptrResponse(req.Message, 0, 0xC00C)
	})

	for i := 0; i < 3; i++ {
		ans, err := p.Resolve(context.Background(), "zero.example.com", dnsmsg.TypeA)
		if err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
		if ans.Cached {
			t.Fatal("TTL=0 answer marked cached")
		}
		if ans.TTL != 0 {
			t.Fatalf("TTL = %d, want 0", ans.TTL)
		}
	}
	if env.srv.RequestsReceived() != 3 {
		t.Fatalf("want 3 upstream calls for TTL=0, got %d", env.srv.RequestsReceived())
	}
	if p.Cache().Len() != 0 {
		t.Fatalf("cache Len = %d, want 0", p.Cache().Len())
	}
}

// 最大合法 TTL 边界：应正常缓存；最高位置 1 的 TTL 必须被拒绝。
func TestTTLMaxAndMSB(t *testing.T) {
	p, env := newEnv(t)

	env.setHandler(func(req fakedns.Request) []byte {
		return ptrResponse(req.Message, 0x7fffffff, 0xC00C)
	})
	ans, err := p.Resolve(context.Background(), "long.example.com", dnsmsg.TypeA)
	if err != nil {
		t.Fatalf("max TTL rejected: %v", err)
	}
	if ans.TTL != 0x7fffffff {
		t.Fatalf("TTL = %#x", ans.TTL)
	}
	if p.Cache().Len() != 1 {
		t.Fatalf("max-TTL entry not cached")
	}

	// 换一个名字，上游返回 MSB 置 1 的 TTL。
	env.setHandler(func(req fakedns.Request) []byte {
		return ptrResponse(req.Message, 0x80000000, 0xC00C)
	})
	_, err = p.Resolve(context.Background(), "badttl.example.com", dnsmsg.TypeA)
	if err == nil || !strings.Contains(err.Error(), "TTL") {
		t.Fatalf("want TTL error, got %v", err)
	}
}

// 指针环（自指）：解析必须失败且不缓存。
func TestUpstreamPointerLoop(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		// 计算 answer name 偏移，让指针指向自身。
		qnameLen := 0
		for _, lab := range strings.Split("loop.example.com.", ".") {
			if lab == "" {
				break
			}
			qnameLen += 1 + len(lab)
		}
		qnameLen++ // root zero
		ansOff := uint16(12 + qnameLen + 4)
		return ptrResponse(req.Message, 60, 0xC000|ansOff)
	})

	_, err := p.Resolve(context.Background(), "loop.example.com", dnsmsg.TypeA)
	if err == nil {
		t.Fatal("pointer loop accepted")
	}
	if p.Cache().Len() != 0 {
		t.Fatalf("error response cached, Len=%d", p.Cache().Len())
	}
}

// 压缩指针越界：指向报文末尾之后。
func TestUpstreamPointerOutOfBounds(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		return ptrResponse(req.Message, 60, 0xC0FF)
	})
	_, err := p.Resolve(context.Background(), "oob.example.com", dnsmsg.TypeA)
	if err == nil || !strings.Contains(err.Error(), "malformed") {
		t.Fatalf("want malformed response error, got %v", err)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("malformed response cached")
	}
}

// 物理截断：RDLENGTH 声称超出报文。
func TestUpstreamTruncatedRDLength(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		resp := ptrResponse(req.Message, 60, 0xC00C)
		qnameLen := 0
		for _, lab := range strings.Split("cut.example.com.", ".") {
			if lab == "" {
				break
			}
			qnameLen += 1 + len(lab)
		}
		qnameLen++
		rlOff := 12 + qnameLen + 4 + 10                      // name ptr(2)+TYPE(2)+CLASS(2)+TTL(4)
		binary.BigEndian.PutUint16(resp[rlOff:rlOff+2], 100) // 只有 4 字节 rdata
		return resp
	})
	_, err := p.Resolve(context.Background(), "cut.example.com", dnsmsg.TypeA)
	if err == nil {
		t.Fatal("truncated rdata accepted")
	}
	if p.Cache().Len() != 0 {
		t.Fatal("truncated response cached")
	}
}

// TC 位置位：UDP-only 代理必须拒绝（无法走 TCP 续传）。
func TestUpstreamTCBit(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		resp := ptrResponse(req.Message, 60, 0xC00C)
		flags := binary.BigEndian.Uint16(resp[2:4])
		binary.BigEndian.PutUint16(resp[2:4], flags|0x0200)
		return resp
	})
	_, err := p.Resolve(context.Background(), "tc.example.com", dnsmsg.TypeA)
	if err == nil || !strings.Contains(err.Error(), "truncated") {
		t.Fatalf("want truncation error, got %v", err)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("truncated response cached")
	}
}

// ID 不匹配：上游回包使用了别的事务 ID。
func TestUpstreamIDMismatch(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		resp := goodResponse(req.Message)
		binary.BigEndian.PutUint16(resp[0:2], req.Message.ID^0xFFFF)
		return resp
	})
	_, err := p.Resolve(context.Background(), "id.example.com", dnsmsg.TypeA)
	if err == nil || !strings.Contains(err.Error(), "transaction ID") {
		t.Fatalf("want ID mismatch error, got %v", err)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("ID-mismatched response cached")
	}
}

// 问题名不匹配：响应的问题段回答了别的名字。
func TestUpstreamNameMismatch(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		q := req.Message.Questions[0]
		q.Name = "other.example.net."
		return goodResponse(&dnsmsg.Message{ID: req.Message.ID, Questions: []dnsmsg.Question{q}})
	})
	_, err := p.Resolve(context.Background(), "want.example.com", dnsmsg.TypeA)
	if err == nil || !strings.Contains(err.Error(), "name") {
		t.Fatalf("want name mismatch error, got %v", err)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("name-mismatched response cached")
	}
}

// NXDOMAIN：合法错误响应，不得缓存。
func TestUpstreamNXDOMAINNotCached(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte {
		resp, _ := dnsmsg.BuildResponse(req.Message.ID, req.Message.Questions[0], 3, false, nil)
		return resp
	})
	ans, err := p.Resolve(context.Background(), "nx.example.com", dnsmsg.TypeA)
	if err != nil {
		t.Fatalf("NXDOMAIN should be a response, not transport error: %v", err)
	}
	if ans.RCode != 3 || len(ans.Answers) != 0 {
		t.Fatalf("unexpected NXDOMAIN answer: %+v", ans)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("NXDOMAIN was cached")
	}
	// 再查一次必须重新访问上游。
	if _, err := p.Resolve(context.Background(), "nx.example.com", dnsmsg.TypeA); err != nil {
		t.Fatal(err)
	}
	if env.srv.RequestsReceived() != 2 {
		t.Fatalf("want 2 upstream calls for NXDOMAIN, got %d", env.srv.RequestsReceived())
	}
}

// 上游无应答：超时错误，不缓存，错误分类为 timeout。
func TestUpstreamTimeout(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte { return nil }) // 丢包

	_, err := p.Resolve(context.Background(), "silent.example.com", dnsmsg.TypeA)
	var pe *proxy.Error
	if !errors.As(err, &pe) || pe.Kind != proxy.KindTimeout {
		t.Fatalf("want KindTimeout, got %v", err)
	}
	if p.Cache().Len() != 0 {
		t.Fatal("timeout outcome cached")
	}
}

// 输入校验：非法类型/名称直接 400 类错误，不访问上游。
func TestInvalidInput(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte { return goodResponse(req.Message) })

	if _, err := p.Resolve(context.Background(), "bad name", dnsmsg.TypeA); err == nil {
		t.Fatal("invalid name accepted")
	}
	if _, err := p.Resolve(context.Background(), "example.com", 99); err == nil {
		t.Fatal("unsupported qtype accepted")
	}
	if env.srv.RequestsReceived() != 0 {
		t.Fatalf("upstream must not be called for invalid input, got %d", env.srv.RequestsReceived())
	}
}

func TestAAAAHappyPath(t *testing.T) {
	p, env := newEnv(t)
	env.setHandler(func(req fakedns.Request) []byte { return goodResponse(req.Message) })

	ans, err := p.Resolve(context.Background(), "example.com", dnsmsg.TypeAAAA)
	if err != nil {
		t.Fatal(err)
	}
	if len(ans.Answers) != 1 || ans.Answers[0].IP.String() != "2001:db8::10" {
		t.Fatalf("unexpected AAAA answer: %+v", ans)
	}
}
