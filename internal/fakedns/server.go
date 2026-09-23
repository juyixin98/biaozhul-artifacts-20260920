// Package fakedns 提供一个本地可控的假 DNS 上游服务器（UDP），
// 供代理联调与测试使用，不访问公网。
//
// 通过 Handler 可以完全控制应答内容，便于构造截断、畸形压缩指针、
// ID 不匹配、TTL 边界等各种响应；ZoneHandler 提供常用的静态分区。
package fakedns

import (
	"net"
	"sync"
	"sync/atomic"

	"dnsproxy/internal/dnsmsg"
)

// Request 是服务器收到的一个已解析查询。
type Request struct {
	Raw     []byte
	Addr    *net.UDPAddr
	Message *dnsmsg.Message
}

// Handler 决定如何应答：返回 nil 表示不回应（模拟丢包）。
type Handler func(req Request) []byte

// Server 是一个 UDP 假 DNS 服务器。
type Server struct {
	conn    *net.UDPConn
	handler Handler
	stop    chan struct{}
	wg      sync.WaitGroup

	requestsReceived int64
	parseFailures    int64
}

// Start 在本地 UDP addr（如 "127.0.0.1:0"）上启动服务器。
func Start(addr string, handler Handler) (*Server, error) {
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
		handler: handler,
		stop:    make(chan struct{}),
	}
	s.wg.Add(1)
	go s.serve()
	return s, nil
}

// Addr 返回实际监听地址（端口可能由内核分配）。
func (s *Server) Addr() string {
	return s.conn.LocalAddr().String()
}

// RequestsReceived 返回收到的数据报数量。
func (s *Server) RequestsReceived() int { return int(atomic.LoadInt64(&s.requestsReceived)) }

// ParseFailures 返回无法解析为 DNS 报文的数据报数量。
func (s *Server) ParseFailures() int { return int(atomic.LoadInt64(&s.parseFailures)) }

func (s *Server) serve() {
	defer s.wg.Done()
	buf := make([]byte, 4096)
	for {
		n, addr, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-s.stop:
				return
			default:
				return
			}
		}
		atomic.AddInt64(&s.requestsReceived, 1)
		pkt := make([]byte, n)
		copy(pkt, buf[:n])

		msg, perr := dnsmsg.ParseMessage(pkt)
		if perr != nil {
			atomic.AddInt64(&s.parseFailures, 1)
		}

		req := Request{Raw: pkt, Addr: addr, Message: msg}
		// 独立 goroutine 处理，避免慢 handler 阻塞读循环。
		go func() {
			resp := s.handler(req)
			if resp == nil {
				return
			}
			if perr != nil {
				// 查询本身畸形时不做应答，避免在垃圾数据上猜测。
				return
			}
			_, _ = s.conn.WriteToUDP(resp, addr)
		}()
	}
}

// Close 停止服务器并关闭 socket。
func (s *Server) Close() error {
	close(s.stop)
	err := s.conn.Close()
	s.wg.Wait()
	return err
}

// ZoneRecord 是静态分区里的一条记录。
type ZoneRecord struct {
	Type uint16
	TTL  uint32
	IP   net.IP
}

// ZoneHandler 返回一个按精确名称匹配的静态应答 handler。
//
// name -> 记录列表：返回 NOERROR 应答；
// miss 返回 NXDOMAIN；未知名称同样 NXDOMAIN；
// 非 A/AAAA 问题返回 NotImplemented(RCODE=4)。
// 非查询报文（QR=1 等）被忽略。
func ZoneHandler(zone map[string][]ZoneRecord) Handler {
	return func(req Request) []byte {
		m := req.Message
		if m == nil || m.QR || m.OpCode != 0 || len(m.Questions) == 0 {
			return nil
		}
		q := m.Questions[0]
		if q.Type != dnsmsg.TypeA && q.Type != dnsmsg.TypeAAAA {
			resp, _ := dnsmsg.BuildResponse(m.ID, q, 4, false, nil) // Not Implemented
			return resp
		}
		records, ok := zone[dnsmsg.CanonicalName(q.Name)]
		if !ok {
			resp, _ := dnsmsg.BuildResponse(m.ID, q, 3, false, nil) // NXDOMAIN
			return resp
		}
		var answers []dnsmsg.AnswerSpec
		for _, r := range records {
			if r.Type != q.Type {
				continue
			}
			answers = append(answers, dnsmsg.AnswerSpec{Name: q.Name, TTL: r.TTL, IP: r.IP})
		}
		resp, _ := dnsmsg.BuildResponse(m.ID, q, 0, false, answers)
		return resp
	}
}
