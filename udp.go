package rt

import (
	"context"
	"net"
	"sync"
	"time"
)

// UDPEndpoint 用真实 net.UDPConn 承载 Link。
// 两个端点互指对端地址（典型为 127.0.0.1 的两个端口，即 UDP 回环）。
type UDPEndpoint struct {
	conn *net.UDPConn
	peer *net.UDPAddr
	inj  *Injector

	closeMu sync.Mutex
	closed  bool
}

// UDPOptions 控制可选行为；零值不可用，请用 NewUDPLoopbackPair 构造。
type UDPOptions struct {
	Faults  PipeFaults
	Clock   Clock         // nil 时用 RealClock
	HoldMax time.Duration // 乱序扣留的最长时间，<=0 用默认 5ms
}

// NewUDPLoopbackPair 在本机创建一对互联的真实 UDP 端点并挂上故障层。
// 同一套 FaultPolicy 同时用于内存链路与 UDP 链路。
func NewUDPLoopbackPair(opts UDPOptions) (a, b Link, err error) {
	clock := opts.Clock
	if clock == nil {
		clock = RealClock{}
	}
	holdMax := opts.HoldMax
	if holdMax <= 0 {
		holdMax = 5 * time.Millisecond
	}
	// 乱序扣留延迟上界由故障层的注入时钟统一驱动。
	f := opts.Faults
	f.AB.ReorderDelay = holdMax
	f.BA.ReorderDelay = holdMax

	ca, addrA, err := listenUDP()
	if err != nil {
		return nil, nil, err
	}
	cb, addrB, err := listenUDP()
	if err != nil {
		ca.Close()
		return nil, nil, err
	}

	epA := &UDPEndpoint{conn: ca, peer: addrB}
	epB := &UDPEndpoint{conn: cb, peer: addrA}
	epA.inj = NewInjector(f.AB, clock, epA.udpDeliver)
	epB.inj = NewInjector(f.BA, clock, epB.udpDeliver)
	return epA, epB, nil
}

func listenUDP() (*net.UDPConn, *net.UDPAddr, error) {
	addr, err := net.ResolveUDPAddr("udp", "127.0.0.1:0")
	if err != nil {
		return nil, nil, err
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return nil, nil, err
	}
	local, ok := conn.LocalAddr().(*net.UDPAddr)
	if !ok {
		conn.Close()
		return nil, nil, net.ErrClosed
	}
	return conn, local, nil
}

// 把一个报文真正写到对端 UDP 地址。
func (e *UDPEndpoint) udpDeliver(p *Packet) {
	b, err := p.Marshal()
	if err != nil || len(b) > UDPMaxPayload {
		return
	}
	// UDP 无连接；即使本端已标记关闭，最后的 ACK/FINACK 也尽量发出。
	_, _ = e.conn.WriteToUDP(b, e.peer)
}

func (e *UDPEndpoint) Send(p *Packet) error {
	e.closeMu.Lock()
	closed := e.closed
	e.closeMu.Unlock()
	if closed {
		return ErrLinkClosed
	}
	e.inj.Egress(p)
	return nil
}

func (e *UDPEndpoint) Recv(ctx context.Context) (*Packet, error) {
	buf := make([]byte, UDPMaxPayload+1)
	for {
		// 让 ctx 取消可以打断阻塞中的读。
		deadline := time.Now().Add(time.Second)
		if dl, ok := ctx.Deadline(); ok && dl.Before(deadline) {
			deadline = dl
		}
		if err := e.conn.SetReadDeadline(deadline); err != nil {
			if e.isClosed() {
				return nil, ErrLinkClosed
			}
			return nil, err
		}
		n, _, err := e.conn.ReadFromUDP(buf)
		if err != nil {
			if e.isClosed() {
				return nil, ErrLinkClosed
			}
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue // 周期性醒来检查 ctx / closed
			}
			return nil, err
		}
		if n > UDPMaxPayload {
			continue
		}
		p, perr := UnmarshalPacket(buf[:n])
		if perr != nil {
			continue // 丢弃畸形报文
		}
		return p, nil
	}
}

func (e *UDPEndpoint) isClosed() bool {
	e.closeMu.Lock()
	defer e.closeMu.Unlock()
	return e.closed
}

func (e *UDPEndpoint) Close() error {
	e.closeMu.Lock()
	if e.closed {
		e.closeMu.Unlock()
		return nil
	}
	e.closed = true
	e.closeMu.Unlock()

	// 先冲刷乱序缓存（关闭后仍尝试把最后的 ACK/FINACK 写入 UDP），
	// 再关闭 socket。
	e.inj.Flush()
	e.inj.Close()
	return e.conn.Close()
}

// Stats 返回本端外发方向的故障计数。
func (e *UDPEndpoint) Stats() FaultStats { return e.inj.Stats() }

// UDPStats 读取 UDP 端点对的故障计数；非 UDP 端点返回 false。
func UDPStats(a, b Link) (PipeStats, bool) {
	ea, ok1 := a.(*UDPEndpoint)
	eb, ok2 := b.(*UDPEndpoint)
	if !ok1 || !ok2 {
		return PipeStats{}, false
	}
	return PipeStats{AB: ea.inj.Stats(), BA: eb.inj.Stats()}, true
}
