package rt

import (
	"bytes"
	"context"
	"errors"
	"io"
	"runtime"
	"testing"
	"time"
)

// patternData 生成确定性的文件内容（线性同余）。
func patternData(n int) []byte {
	b := make([]byte, n)
	x := uint32(0x12345678)
	for i := 0; i < n; i++ {
		x = x*1103515245 + 12345
		b[i] = byte(x >> 16)
	}
	return b
}

func fakeCfg() Config {
	return Config{
		MSS:        256,
		WindowSize: 6,
		RTO:        5 * time.Millisecond,
		MaxRetries: 200,
		StartSeq:   100,
		Clock:      NewFakeClock(),
	}
}

type transferOut struct {
	res TransferResult
	err error
}

// runFake 在内存链路上跑一次传输，同时手工推进虚拟时钟。
func runFake(t *testing.T, data []byte, cfg Config, faults PipeFaults) (transferOut, []byte) {
	t.Helper()
	clk := cfg.Clock.(*FakeClock)
	a, b := PipeWithClock(faults, clk)
	var got bytes.Buffer
	done := make(chan transferOut, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	go func() {
		r, err := RunTransfer(ctx, a, b, 42, cfg, bytes.NewReader(data), &got)
		done <- transferOut{r, err}
	}()
	deadline := time.Now().Add(20 * time.Second)
	steps := 0
	for {
		select {
		case o := <-done:
			return o, got.Bytes()
		default:
		}
		pumpStep(clk)
		steps++
		if time.Now().After(deadline) {
			t.Fatalf("transfer hung in real time (virtual now %s, steps %d)", clk.Now(), steps)
		}
	}
}

// pumpStep 把虚拟时钟推进一小步；当有定时器真正触发时，睡一小段
// 真实时间，让协议 goroutine（-race 下会明显变慢）在下一次虚拟到期前
// 把触发产生的报文处理完，避免虚拟时钟冲得比真实处理快。
func pumpStep(clk *FakeClock) {
	fired := clk.Step(time.Millisecond)
	if fired {
		time.Sleep(2 * time.Millisecond)
	} else if !clk.HasPending() {
		time.Sleep(100 * time.Microsecond)
	}
	runtime.Gosched()
}

func ghostsFor(gen uint64, n int) []*Packet {
	var g []*Packet
	// 旧连接 SYN + 旧连接数据/ACK，制造各种“上一代连接”的残留。
	for i := 0; i < n; i++ {
		switch i % 3 {
		case 0:
			g = append(g, &Packet{Type: MsgSYN, Gen: gen - 1, Payload: marshalSYN(uint32(i), 256)})
		case 1:
			g = append(g, &Packet{Type: MsgDATA, Gen: gen - 1, Seq: uint32(i), Payload: []byte("stale")})
		default:
			g = append(g, &Packet{Type: MsgFINACK, Gen: gen - 1, Ack: uint32(i)})
		}
	}
	return g
}

// 验收 1：干净链路，多种大小（含空文件）逐字节一致。
func TestTransferClean(t *testing.T) {
	sizes := []int{0, 1, 255, 256, 257, 4096, 12000}
	for _, n := range sizes {
		data := patternData(n)
		cfg := fakeCfg()
		o, got := runFake(t, data, cfg, PipeFaults{})
		if o.err != nil {
			t.Fatalf("size %d: %v", n, o.err)
		}
		if !bytes.Equal(got, data) {
			t.Fatalf("size %d: payload mismatch", n)
		}
		if o.res.Size != int64(n) {
			t.Fatalf("size %d: result size %d", n, o.res.Size)
		}
		if !bytes.Equal(o.res.SenderHash, o.res.ReceiverHash) {
			t.Fatalf("size %d: hash mismatch in result", n)
		}
		if o.res.Receiver.StalePackets != 0 {
			t.Fatalf("size %d: unexpected stale packets %+v", n, o.res.Receiver)
		}
		// 注：不要求 OutOfWindow==0。FakeClock 泵偶发在 ACK 在途时
		// 先推进过 RTO，发送方按协议超时重传整个窗口，接收方对重复
		// DATA 回重复 ACK 并计入 OutOfWindow——这是 Go-Back-N 的正确
		// 行为，内容与哈希正确即说明传输语义无误。
	}
}

// 验收 2：固定种子 + 有限预算，同时注入丢包、重复、乱序和旧连接报文，
// 传输必须完成且内容、哈希一致。
func TestAcceptanceFiniteFaults(t *testing.T) {
	const seed = int64(20260924)
	data := patternData(20000) // 79 个 DATA
	cfg := fakeCfg()
	faults := PipeFaults{
		AB: FaultPolicy{
			Seed: seed,
			// 速率设为 1：先由“预算”封顶，预算用完后一切恢复正常，
			// 保证故障有限、传输必然完成。
			LossRate: 1, LossBudget: 15,
			DupRate: 1, DupBudget: 10,
			ReorderRate: 1, ReorderBudget: 8, ReorderHold: 3,
			Ghosts: ghostsFor(42, 9),
		},
		BA: FaultPolicy{
			Seed:     seed ^ 0x5a5a,
			LossRate: 1, LossBudget: 12,
			DupRate: 1, DupBudget: 8,
			ReorderRate: 1, ReorderBudget: 6, ReorderHold: 2,
			Ghosts: ghostsFor(42, 6),
		},
	}
	o, got := runFake(t, data, cfg, faults)
	if o.err != nil {
		t.Fatalf("transfer failed under finite faults: %v", o.err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("payload mismatch under faults")
	}
	if !bytes.Equal(o.res.SenderHash, o.res.ReceiverHash) {
		t.Fatal("hash mismatch under faults")
	}

	// 故障确实发生了（不是“零故障作弊”）。
	if o.res.Faults.AB.Lost != 15 || o.res.Faults.BA.Lost != 12 {
		t.Errorf("loss budgets not consumed: AB=%d BA=%d", o.res.Faults.AB.Lost, o.res.Faults.BA.Lost)
	}
	if o.res.Faults.AB.Reordered == 0 || o.res.Faults.BA.Reordered == 0 {
		t.Error("reordering never happened")
	}
	if o.res.Faults.AB.Duplicated == 0 || o.res.Faults.BA.Duplicated == 0 {
		t.Error("duplication never happened")
	}
	if o.res.Faults.AB.GhostSent == 0 || o.res.Faults.BA.GhostSent == 0 {
		t.Error("ghosts never sent")
	}
	// 旧代际报文必须全部被忽略，传输结果仍是正确文件（隐式），
	// 且接收端统计到了代际不符。
	if o.res.Receiver.StalePackets == 0 {
		t.Error("receiver recorded no stale packets")
	}
	// 重传与重复 ACK 确实被触发。
	if o.res.Sender.Retransmits == 0 {
		t.Error("no retransmits happened despite losses")
	}
	if o.res.Receiver.OutOfWindow == 0 {
		t.Error("receiver never saw out-of-order/dup data")
	}
}

// 验收 3：无限丢包（速率 1、预算 -1）传输永远完不成，
// MaxRetries 必须让它有界地失败，而不是挂死。
func TestUnboundedLossFailsBounded(t *testing.T) {
	cfg := fakeCfg()
	cfg.MaxRetries = 5
	o, _ := runFake(t, patternData(1000), cfg, PipeFaults{
		AB: FaultPolicy{Seed: 1, LossRate: 1, LossBudget: -1},
	})
	// 有界失败即可：要么重传计数触顶 ErrMaxRetries，要么随后链路关闭
	// 得到 ErrLinkClosed。绝不允许挂死。
	if !errors.Is(o.err, ErrMaxRetries) && !errors.Is(o.err, ErrLinkClosed) {
		t.Fatalf("err=%v, want a bounded failure", o.err)
	}
}

// 验收 4：固定种子、非 1 的概率 + 无限预算，统计意义上的重压力测试。
// 每个方向再挂少量幽灵报文。
func TestAcceptanceStochastic(t *testing.T) {
	data := patternData(60000) // 235 个 DATA
	cfg := fakeCfg()
	cfg.MSS = 128
	faults := PipeFaults{
		AB: FaultPolicy{
			Seed:     777,
			LossRate: 0.25, LossBudget: -1,
			DupRate: 0.1, DupBudget: -1,
			ReorderRate: 0.15, ReorderBudget: -1, ReorderHold: 2,
			Ghosts: ghostsFor(42, 4),
		},
		BA: FaultPolicy{
			Seed:     999,
			LossRate: 0.3, LossBudget: -1,
			DupRate: 0.1, DupBudget: -1,
			ReorderRate: 0.1, ReorderBudget: -1, ReorderHold: 2,
			Ghosts: ghostsFor(42, 4),
		},
	}
	o, got := runFake(t, data, cfg, faults)
	if o.err != nil {
		t.Fatalf("stochastic transfer failed: %v", o.err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("stochastic payload mismatch")
	}
	if o.res.Faults.AB.Lost == 0 || o.res.Faults.BA.Lost == 0 {
		t.Error("expected random losses")
	}
}

// 验收 5：窗口内存高水位有严格上界（恒 <= WindowSize 个报文，
// 字节 <= WindowSize*MSS）。
func TestWindowMemoryBounded(t *testing.T) {
	data := patternData(50000)
	cfg := fakeCfg()
	cfg.WindowSize = 5
	cfg.MSS = 256
	faults := PipeFaults{
		AB: FaultPolicy{Seed: 5, LossRate: 0.4, LossBudget: -1,
			ReorderRate: 0.3, ReorderBudget: -1, ReorderHold: 2},
		BA: FaultPolicy{Seed: 6, LossRate: 0.4, LossBudget: -1},
	}
	o, got := runFake(t, data, cfg, faults)
	if o.err != nil {
		t.Fatal(o.err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("payload mismatch")
	}
	if o.res.Sender.MaxBufferedPackets > cfg.WindowSize {
		t.Errorf("MaxBufferedPackets=%d > window %d", o.res.Sender.MaxBufferedPackets, cfg.WindowSize)
	}
	if o.res.Sender.MaxBufferedBytes > int64(cfg.WindowSize*cfg.MSS) {
		t.Errorf("MaxBufferedBytes=%d > cap %d", o.res.Sender.MaxBufferedBytes, cfg.WindowSize*cfg.MSS)
	}
}

// 多种子压力扫描：每个种子故障模式不同（概率+无限预算），全部必须完成。
// 使用 FakeClock，运行快且确定性可复现。
func TestManySeedsStress(t *testing.T) {
	data := patternData(15000)
	for seed := int64(1); seed <= 40; seed++ {
		cfg := fakeCfg()
		faults := PipeFaults{
			AB: FaultPolicy{
				Seed:     seed * 31,
				LossRate: 0.35, LossBudget: -1,
				DupRate: 0.12, DupBudget: -1,
				ReorderRate: 0.18, ReorderBudget: -1, ReorderHold: 2,
				Ghosts: ghostsFor(42, 2),
			},
			BA: FaultPolicy{
				Seed:     seed*31 + 7,
				LossRate: 0.4, LossBudget: -1,
				DupRate: 0.1, DupBudget: -1,
				ReorderRate: 0.12, ReorderBudget: -1, ReorderHold: 2,
				Ghosts: ghostsFor(42, 2),
			},
		}
		o, got := runFake(t, data, cfg, faults)
		if o.err != nil {
			t.Fatalf("seed %d: %v", seed, o.err)
		}
		if !bytes.Equal(got, data) {
			t.Fatalf("seed %d: payload mismatch", seed)
		}
	}
}

// 序号回绕：起始序号接近 2^32，传输中必须正确处理 wrap-around。
func TestSequenceWraparound(t *testing.T) {
	data := patternData(4096)
	cfg := fakeCfg()
	cfg.MSS = 200 // 21 个 DATA，跨过 2^32 边界
	cfg.WindowSize = 4
	cfg.StartSeq = 0xFFFFFFFF - 5
	faults := PipeFaults{
		// 概率丢包（非必中）+ 无限预算：整个过程持续丢包，数据阶段
		// 必然走上超时/快速重传，同时传输最终仍能收敛完成。
		AB: FaultPolicy{Seed: 11, LossRate: 0.4, LossBudget: -1,
			ReorderRate: 0.2, ReorderBudget: -1, ReorderHold: 2},
		BA: FaultPolicy{Seed: 12, LossRate: 0.4, LossBudget: -1},
	}
	o, got := runFake(t, data, cfg, faults)
	if o.err != nil {
		t.Fatalf("wraparound transfer failed: %v", o.err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("wraparound payload mismatch")
	}
	if o.res.Sender.Retransmits == 0 {
		t.Error("wraparound test expected DATA retransmits")
	}
	// 21 个 DATA 从 0xFFFFFFFA 起编，最后序号为 0x0000000F，确认跨越边界。
	var startW uint32 = 0xFFFFFFFF - 5
	if got := startW + 21; got != 0x0000000F {
		t.Fatalf("test arithmetic wrong: final seq=%08x", got)
	}
}

// 连接代际：用 gen=42 建链，对端链路上混入 gen=41/43 的报文，
// 传输内容仍必须正确（部分在其他用例已覆盖，这里用错配代际直接跑，
// 验证代际不一致时连接无法错误建立、最终失败有界）。
func TestGenerationIsolation(t *testing.T) {
	clk := NewFakeClock()
	cfg := Config{MSS: 128, WindowSize: 4, RTO: 5 * time.Millisecond,
		MaxRetries: 5, StartSeq: 0, Clock: clk}
	a, b := Pipe(PipeFaults{})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	type senderErr struct{ err error }
	sdone := make(chan senderErr, 1)
	go func() {
		_, _, err := SendFile(ctx, a, 50, cfg, bytes.NewReader(patternData(500)), 500)
		sdone <- senderErr{err}
	}()
	// 接收方等待的是 gen=51：永远不应接受 gen=50 的连接。
	rdone := make(chan error, 1)
	go func() {
		_, _, _, err := ReceiveFile(ctx, b, 51, cfg, io.Discard)
		rdone <- err
	}()

	deadline := time.Now().Add(10 * time.Second)
	for {
		select {
		case se := <-sdone:
			if !errors.Is(se.err, ErrMaxRetries) {
				t.Fatalf("sender err=%v want ErrMaxRetries (SYN never accepted)", se.err)
			}
			_ = a.Close()
			_ = b.Close()
			rerr := <-rdone
			if rerr == nil {
				t.Fatal("receiver with mismatched gen should not complete")
			}
			return
		default:
		}
		pumpStep(clk)
		if time.Now().After(deadline) {
			t.Fatal("generation isolation test hung")
		}
	}
}

// 整体哈希：数据被篡改时接收方必须通过 FIN 整体哈希发现并拒绝。
type mutateLink struct {
	Link
	mutated bool
}

// Recv 篡改第一个有载荷的 DATA 报文，模拟数据被破坏但 UDP 校验未检出
// （或协议无逐包校验的情形），应由 FIN 整体哈希发现。
func (m *mutateLink) Recv(ctx context.Context) (*Packet, error) {
	p, err := m.Link.Recv(ctx)
	if err != nil {
		return p, err
	}
	if !m.mutated && p.Type == MsgDATA && len(p.Payload) > 2 {
		p = clonePacket(p)
		p.Payload[2] ^= 0xFF
		m.mutated = true
	}
	return p, nil
}

func TestHashMismatchDetected(t *testing.T) {
	clk := NewFakeClock()
	cfg := Config{MSS: 256, WindowSize: 4, RTO: 5 * time.Millisecond,
		MaxRetries: 50, StartSeq: 0, Clock: clk}
	a, b := Pipe(PipeFaults{})
	mb := &mutateLink{Link: b}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	type res struct{ err error }
	done := make(chan res, 1)
	go func() {
		_, _, _, err := ReceiveFile(ctx, mb, 42, cfg, io.Discard)
		done <- res{err}
	}()
	go func() {
		_, _, _ = SendFile(ctx, a, 42, cfg, bytes.NewReader(patternData(2000)), 2000)
		_ = a.Close()
	}()

	deadline := time.Now().Add(10 * time.Second)
	for {
		select {
		case r := <-done:
			if !errors.Is(r.err, ErrHashMismatch) {
				t.Fatalf("err=%v want ErrHashMismatch", r.err)
			}
			_ = b.Close()
			return
		default:
		}
		pumpStep(clk)
		if time.Now().After(deadline) {
			t.Fatal("hash mismatch test hung")
		}
	}
}
