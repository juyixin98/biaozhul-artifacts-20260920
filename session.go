package rt

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"time"
)

// ErrHashMismatch 表示整体哈希校验失败。
var ErrHashMismatch = errors.New("rt: whole-file hash mismatch")

// ErrMaxRetries 表示重传次数超过上限（故障不是“有限”的）。
var ErrMaxRetries = errors.New("rt: max retransmissions exceeded")

// Config 是一次传输的参数。
type Config struct {
	MSS        int           // 数据块大小（字节）
	WindowSize int           // 滑动窗口大小（报文个数）
	RTO        time.Duration // 重传超时
	MaxRetries int           // 单阶段最多连续超时次数（接收方挥手等待轮数）
	StartSeq   uint32        // SYN 提议的起始数据序号（测试回绕用）
	Clock      Clock         // nil 用 RealClock
}

func (c *Config) normalize() {
	if c.MSS <= 0 {
		c.MSS = 1024
	}
	if c.WindowSize <= 0 {
		c.WindowSize = 8
	}
	if c.RTO <= 0 {
		c.RTO = 25 * time.Millisecond
	}
	if c.MaxRetries <= 0 {
		c.MaxRetries = 50
	}
	if c.Clock == nil {
		c.Clock = RealClock{}
	}
}

// SenderStats 是发送方侧的统计。
type SenderStats struct {
	SentPackets        int   // 首次发送的报文数（SYN/DATA/FIN）
	Retransmits        int   // 重传份数合计（SYN/FIN 重发 + DATA 重传）
	TimeoutRetransmits int   // RTO 超时触发的 DATA 重传份数
	FastRetransmits    int   // 3 个重复 ACK 触发的重传份数
	Timeouts           int   // RTO 超时次数
	DupACKs            int   // 收到的重复 ACK 次数
	MaxBufferedPackets int   // 发送窗口内存高水位（报文数，恒 <= WindowSize）
	MaxBufferedBytes   int64 // 发送窗口内存高水位（字节）
	BytesRead          int64 // 从输入读取的字节数（= 文件大小）
}

// ReceiverStats 是接收方侧的统计。
type ReceiverStats struct {
	InOrderPackets int   // 按序上交的数据报文数
	OutOfWindow    int   // 乱序超前/重复落后而丢弃的数据报文数（均回 ACK）
	StalePackets   int   // 连接代际不符而被忽略的报文数
	ACKsSent       int   // 发出的 ACK/FINACK 次数
	BytesWritten   int64 // 写入接收端的数据字节数
}

// TransferResult 是一次完整传输的结果。
type TransferResult struct {
	Size         int64
	StartSeq     uint32
	MSS          int
	SenderHash   []byte // 发送端流式算出的 SHA-256
	ReceiverHash []byte // 接收端流式算出的 SHA-256
	Sender       SenderStats
	Receiver     ReceiverStats
	Faults       PipeStats
}

type packetOrErr struct {
	p   *Packet
	err error
}

type sender struct {
	link Link
	gen  uint64
	cfg  Config
	r    io.Reader
	h    interface{ Write([]byte) (int, error) } // 发送端整体哈希

	base          uint32 // 最早未确认序号 una
	next          uint32 // 下一个待发序号
	ring          [][]byte
	buffered      int
	bufferedBytes int64
	eof           bool
	bytesRead     int64

	timer         Timer
	timeoutStreak int

	stat SenderStats
	recv chan packetOrErr
}

// SendFile 运行发送方状态机：SYN 建连 → 滑窗传数据 → FIN（总长+整体哈希）。
// 读取 r 时流式计算 SHA-256，随 FIN 发出；totalSize 为已知大小时传入（<=0 未知，
// 实际仍以读取字节为准）。
func SendFile(ctx context.Context, link Link, gen uint64, cfg Config, r io.Reader, totalSize int64) (SenderStats, []byte, error) {
	cfg.normalize()
	s := &sender{
		link: link, gen: gen, cfg: cfg, r: r,
		base: cfg.StartSeq, next: cfg.StartSeq,
		ring: make([][]byte, cfg.WindowSize),
		h:    sha256.New(),
		recv: make(chan packetOrErr, 16),
	}
	s.startReader(ctx)
	err := s.run(ctx)
	sum := s.h.(interface {
		Sum([]byte) []byte
	}).Sum(nil)
	return s.stat, sum, err
}

// startReader 用一个固定 goroutine 串行读 Link，避免主循环重入造成 goroutine 堆积。
func (s *sender) startReader(ctx context.Context) {
	go func() {
		for {
			p, err := s.link.Recv(ctx)
			select {
			case s.recv <- packetOrErr{p: p, err: err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
}

func (s *sender) send(p *Packet) error {
	s.stat.SentPackets++
	return s.link.Send(p)
}

func (s *sender) sendRaw(p *Packet) error { return s.link.Send(p) }

func (s *sender) stopTimer() {
	if s.timer != nil {
		s.timer.Stop()
		s.timer = nil
	}
}

func (s *sender) armTimer() {
	s.stopTimer()
	s.timer = s.cfg.Clock.NewTimer(s.cfg.RTO)
}

func (s *sender) timerC() <-chan time.Time {
	if s.timer == nil {
		return nil
	}
	return s.timer.C()
}

func (s *sender) run(ctx context.Context) error {
	// ---- 阶段 1：SYN 建连 ----
	syn := &Packet{Type: MsgSYN, Gen: s.gen, Payload: marshalSYN(s.cfg.StartSeq, s.cfg.MSS)}
	if err := s.send(syn); err != nil {
		return err
	}
	s.armTimer()
established:
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.timerC():
			if err := s.onTimeout(syn); err != nil {
				return err
			}
		case m, ok := <-s.recv:
			if !ok || m.err != nil {
				return s.recvErr(m, ok)
			}
			p := m.p
			if p.Type == MsgERROR {
				return peerError(p)
			}
			if p.Gen != s.gen {
				continue // 旧连接幽灵报文
			}
			if p.Type == MsgSYNACK && p.Ack == s.cfg.StartSeq {
				break established
			}
		}
	}
	s.stopTimer()
	s.timeoutStreak = 0

	// ---- 阶段 2：滑动窗口数据传输 ----
	if err := s.fillWindow(); err != nil && err != io.EOF {
		return err
	}
	if s.buffered > 0 {
		s.armTimer()
	}

	dupACKs := 0
	for !s.eof || s.base != s.next {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.timerC():
			n := s.retransmitAll()
			s.stat.TimeoutRetransmits += n
			s.timeoutStreak++
			if s.timeoutStreak > s.cfg.MaxRetries {
				return ErrMaxRetries
			}
			dupACKs = 0
			s.armTimer()
		case m, ok := <-s.recv:
			if !ok || m.err != nil {
				return s.recvErr(m, ok)
			}
			p := m.p
			if p.Type == MsgERROR {
				return peerError(p)
			}
			if p.Gen != s.gen {
				continue // 旧代际报文：发送端直接忽略
			}
			if p.Type != MsgACK && p.Type != MsgFINACK {
				continue
			}
			ack := p.Ack
			used := windowUsed(s.base, s.next)
			if !seqInWindow(s.base, ack, used+1) {
				continue
			}
			if ack == s.base {
				dupACKs++
				s.stat.DupACKs++
				// 边沿触发：只有“第 3 个”重复 ACK 快速重传一次；
				// 之后的重复 ACK 不再重复触发（否则重传的整窗报文会
				// 各自再产生重复 ACK，形成重传雪崩）。新 ACK 到达时
				// dupACKs 清零，重新计数。
				if dupACKs == 3 {
					n := s.retransmitAll()
					s.stat.FastRetransmits += n
					// 快速重传不重置 RTO 计时器。
				}
				continue
			}
			s.advance(ack)
			dupACKs = 0
			s.timeoutStreak = 0
			if !s.eof {
				if err := s.fillWindow(); err != nil && err != io.EOF {
					return err
				}
			}
			if s.buffered == 0 {
				s.stopTimer()
			} else {
				s.armTimer()
			}
		}
	}

	// ---- 阶段 3：FIN（总长 + 整体哈希）----
	sum := s.h.(interface {
		Sum([]byte) []byte
	}).Sum(nil)
	finSeq := s.next
	fin := &Packet{
		Type: MsgFIN, Gen: s.gen, Seq: finSeq,
		Payload: marshalFIN(uint64(s.bytesRead), sum),
	}
	if err := s.send(fin); err != nil {
		return err
	}
	s.armTimer()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-s.timerC():
			if err := s.onTimeout(fin); err != nil {
				return err
			}
		case m, ok := <-s.recv:
			if !ok || m.err != nil {
				return s.recvErr(m, ok)
			}
			p := m.p
			if p.Type == MsgERROR {
				return peerError(p)
			}
			if p.Gen != s.gen {
				continue
			}
			// 只接受携带 finSeq+1 的 FINACK。不能接受普通 ACK：
			// ACK 方向上可能滞留数据阶段的旧累积 ACK（尤其经乱序缓存
			// 延迟冲刷），其 ack 只等于 finSeq，会被误认为 FIN 已确认。
			if p.Type == MsgFINACK && p.Ack == finSeq+1 {
				s.stopTimer()
				// 四次挥手的最后一握：通知对端它可以结束挥手等待。
				_ = s.sendRaw(&Packet{Type: MsgFINACK, Gen: s.gen, Ack: finSeq + 1})
				return nil
			}
		}
	}
}

func (s *sender) onTimeout(p *Packet) error {
	s.stat.Timeouts++
	s.timeoutStreak++
	if s.timeoutStreak > s.cfg.MaxRetries {
		return ErrMaxRetries
	}
	if err := s.send(p); err != nil {
		return err
	}
	s.armTimer()
	return nil
}

func (s *sender) recvErr(m packetOrErr, ok bool) error {
	if !ok || m.err == nil {
		return ErrLinkClosed
	}
	if errors.Is(m.err, ErrLinkClosed) {
		return ErrLinkClosed
	}
	return m.err
}

func peerError(p *Packet) error {
	if bytes.Contains(p.Payload, []byte("hash mismatch")) {
		return ErrHashMismatch
	}
	return fmt.Errorf("rt: peer error: %s", string(p.Payload))
}

// fillWindow 从 reader 读取数据填满发送窗口；输入结束时返回 io.EOF。
func (s *sender) fillWindow() error {
	for s.buffered < s.cfg.WindowSize {
		buf := make([]byte, s.cfg.MSS)
		n, err := io.ReadFull(s.r, buf)
		if n > 0 {
			buf = buf[:n]
			s.h.Write(buf) // 发送端整体哈希（随读随算）
			s.bytesRead += int64(n)
			s.ring[int(s.next)%s.cfg.WindowSize] = buf
			s.buffered++
			s.bufferedBytes += int64(n)
			if s.buffered > s.stat.MaxBufferedPackets {
				s.stat.MaxBufferedPackets = s.buffered
			}
			if s.bufferedBytes > s.stat.MaxBufferedBytes {
				s.stat.MaxBufferedBytes = s.bufferedBytes
			}
			if err := s.send(&Packet{Type: MsgDATA, Gen: s.gen, Seq: s.next, Payload: buf}); err != nil {
				return err
			}
			s.next++
		}
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			s.eof = true
			s.stat.BytesRead = s.bytesRead
			return io.EOF
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// advance 把 base 滑到 ack 并释放已确认缓存。
func (s *sender) advance(ack uint32) {
	for s.base != ack {
		idx := int(s.base) % s.cfg.WindowSize
		s.bufferedBytes -= int64(len(s.ring[idx]))
		s.ring[idx] = nil
		s.buffered--
		s.base++
	}
}

// retransmitAll 重传窗口内全部未确认 DATA（Go-Back-N），返回份数。
func (s *sender) retransmitAll() int {
	n := 0
	for seq := s.base; seq != s.next; seq++ {
		payload := s.ring[int(seq)%s.cfg.WindowSize]
		_ = s.sendRaw(&Packet{Type: MsgDATA, Gen: s.gen, Seq: seq, Payload: payload})
		n++
	}
	s.stat.Retransmits += n
	return n
}

// ---- 接收方 ----

type receiver struct {
	link Link
	gen  uint64
	cfg  Config
	hw   hashWriter

	connected bool
	startSeq  uint32
	mss       int
	expected  uint32
	written   int64
	gotFIN    bool
	finalAck  uint32

	stat ReceiverStats
}

// ReceiveFile 运行接收方状态机，把收到的数据写入 w 并流式计算 SHA-256。
func ReceiveFile(ctx context.Context, link Link, gen uint64, cfg Config, w io.Writer) (TransferResult, ReceiverStats, []byte, error) {
	cfg.normalize()
	h := sha256.New()
	r := &receiver{
		link: link, gen: gen, cfg: cfg,
		hw: hashWriter{w: w, h: h},
	}
	err := r.run(ctx)
	sum := h.Sum(nil)
	res := TransferResult{
		Size:         r.written,
		StartSeq:     r.startSeq,
		MSS:          r.mss,
		ReceiverHash: sum,
	}
	return res, r.stat, sum, err
}

func (r *receiver) sendACK(ack uint32) error {
	r.stat.ACKsSent++
	return r.link.Send(&Packet{Type: MsgACK, Gen: r.gen, Ack: ack})
}

func (r *receiver) sendFINACK() error {
	r.stat.ACKsSent++
	return r.link.Send(&Packet{Type: MsgFINACK, Gen: r.gen, Ack: r.finalAck})
}

func (r *receiver) run(ctx context.Context) error {
	// 1) 等待本代际的 SYN
	for {
		p, err := r.link.Recv(ctx)
		if err != nil {
			return err
		}
		if p.Type != MsgSYN || p.Gen != r.gen {
			// 建连前的数据/旧代际幽灵报文一律挡在连接之外。
			r.stat.StalePackets++
			continue
		}
		start, mss, perr := unmarshalSYN(p.Payload)
		if perr != nil {
			r.stat.StalePackets++
			continue
		}
		r.connected = true
		r.startSeq, r.mss = start, mss
		r.expected = start
		if err := r.link.Send(&Packet{Type: MsgSYNACK, Gen: r.gen, Ack: start}); err != nil {
			return err
		}
		break
	}

	// 2) 数据循环
	for {
		p, err := r.link.Recv(ctx)
		if err != nil {
			return err
		}
		if p.Gen != r.gen {
			r.stat.StalePackets++
			continue
		}
		switch p.Type {
		case MsgSYN: // SYN 重传：再回一次 SYNACK
			if err := r.link.Send(&Packet{Type: MsgSYNACK, Gen: r.gen, Ack: r.startSeq}); err != nil {
				return err
			}
		case MsgDATA:
			if p.Seq == r.expected {
				if _, err := r.hw.Write(p.Payload); err != nil {
					return err
				}
				r.written += int64(len(p.Payload))
				r.stat.BytesWritten = r.written
				r.expected++
				r.stat.InOrderPackets++
				if err := r.sendACK(r.expected); err != nil {
					return err
				}
			} else {
				// Go-Back-N：乱序超前或重复落后都不接收，立即回重复 ACK。
				r.stat.OutOfWindow++
				if err := r.sendACK(r.expected); err != nil {
					return err
				}
			}
		case MsgFIN:
			return r.handleFIN(ctx, p)
		case MsgERROR:
			return peerError(p)
		case MsgACK, MsgFINACK:
			// 数据阶段收到没有意义，忽略。
		}
	}
}

// handleFIN 处理 FIN：缺口未补齐则先继续收数据，再校验长度与整体哈希。
func (r *receiver) handleFIN(ctx context.Context, p *Packet) error {
	// FIN 重传（挥手等待中对端超时）：重新确认。
	if r.gotFIN {
		return r.waitClose(ctx, p)
	}

	for p.Seq != r.expected {
		if err := r.sendACK(r.expected); err != nil {
			return err
		}
		np, err := r.link.Recv(ctx)
		if err != nil {
			return err
		}
		if np.Gen != r.gen {
			r.stat.StalePackets++
			continue
		}
		if np.Type == MsgDATA {
			if np.Seq == r.expected {
				if _, err := r.hw.Write(np.Payload); err != nil {
					return err
				}
				r.written += int64(len(np.Payload))
				r.stat.BytesWritten = r.written
				r.expected++
				r.stat.InOrderPackets++
				if err := r.sendACK(r.expected); err != nil {
					return err
				}
			} else {
				r.stat.OutOfWindow++
				if err := r.sendACK(r.expected); err != nil {
					return err
				}
			}
			continue
		}
		if np.Type == MsgFIN {
			p = np
			continue
		}
		if np.Type == MsgERROR {
			return peerError(np)
		}
	}

	declared, wantHash, err := unmarshalFIN(p.Payload)
	if err != nil {
		_ = r.link.Send(&Packet{Type: MsgERROR, Gen: r.gen, Payload: []byte("bad FIN metadata")})
		return errors.New("rt: bad FIN metadata")
	}
	gotHash := r.hw.h.Sum(nil)
	if declared != uint64(r.written) || !bytes.Equal(gotHash, wantHash) {
		_ = r.link.Send(&Packet{Type: MsgERROR, Gen: r.gen, Payload: []byte("hash mismatch")})
		return ErrHashMismatch
	}

	r.gotFIN = true
	r.finalAck = r.expected + 1
	if err := r.sendFINACK(); err != nil {
		return err
	}
	return r.waitClose(ctx, p)
}

// waitClose 发出 FINACK 后等待发送方的最终 FINACK；
// FIN 重传则重新确认。等不到（丢失等）则在 linger 超时后自行结束。
func (r *receiver) waitClose(ctx context.Context, fin *Packet) error {
	if r.gotFIN && fin != nil && fin.Type == MsgFIN {
		if err := r.sendFINACK(); err != nil {
			return err
		}
	}
	timer := r.cfg.Clock.NewTimer(r.cfg.RTO)
	rounds := 0
	for {
		recvCh := make(chan packetOrErr, 1)
		go func() {
			p, err := r.link.Recv(ctx)
			recvCh <- packetOrErr{p: p, err: err}
		}()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C():
			rounds++
			if rounds >= r.cfg.MaxRetries {
				return nil
			}
			timer.Reset(r.cfg.RTO)
		case m := <-recvCh:
			if m.err != nil {
				return m.err
			}
			p := m.p
			if p.Gen != r.gen {
				r.stat.StalePackets++
				continue
			}
			switch p.Type {
			case MsgDATA:
				// 极晚到的数据（发送端已重传补齐，这里基本是重复）：
				// 回最终确认。
				r.stat.OutOfWindow++
				if err := r.link.Send(&Packet{Type: MsgFINACK, Gen: r.gen, Ack: r.finalAck}); err != nil {
					return err
				}
			case MsgFIN:
				if err := r.sendFINACK(); err != nil {
					return err
				}
			case MsgFINACK:
				return nil // 发送方的最终确认，干净挥手
			}
		}
	}
}

// RunTransfer 在给定的一对链路端点上跑一次完整回环传输：
// 接收方后台运行，发送方把 r 的全部内容推送过去；
// 双方流式计算整体 SHA-256（接收端还会用 FIN 中的长度/哈希做比对）。
//
// 返回时 a、b 都已关闭。Faults 字段在 faults 为 nil 时为零值。
func RunTransfer(ctx context.Context, a, b Link, gen uint64, cfg Config, r io.Reader, w io.Writer) (TransferResult, error) {
	type recvOut struct {
		res TransferResult
		st  ReceiverStats
		sum []byte
		err error
	}
	done := make(chan recvOut, 1)
	go func() {
		res, st, sum, err := ReceiveFile(ctx, b, gen, cfg, w)
		done <- recvOut{res, st, sum, err}
	}()

	sendStat, sendSum, sendErr := SendFile(ctx, a, gen, cfg, r, 0)

	// 发送方失败时，接收方还在等后续报文——关闭它的链路以解除阻塞，
	// 让接收方带着 ErrLinkClosed 结束。成功时接收方已通过挥手自行结束，
	// 随后关闭两条链路（关闭会冲刷外发方向上残留的乱序缓存）。
	if sendErr != nil {
		_ = a.Close()
		_ = b.Close()
	}
	ro := <-done
	if sendErr == nil {
		_ = a.Close()
	}
	_ = b.Close()

	out := ro.res
	out.Sender = sendStat
	out.SenderHash = sendSum
	out.Receiver = ro.st
	if ro.err != nil {
		return out, ro.err
	}
	if sendErr != nil {
		return out, sendErr
	}
	if !bytes.Equal(sendSum, ro.sum) {
		return out, ErrHashMismatch
	}
	if st, ok := StatsOf(a, b); ok {
		out.Faults = st
	} else if st, ok := UDPStats(a, b); ok {
		out.Faults = st
	}
	return out, nil
}
