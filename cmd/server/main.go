// Command server 启动“UDP 可靠传输模拟”的纯后端 HTTP 服务。
//
// 每次 POST /transfer 都在服务内部建立一对链路端点（内存 Pipe 或
// 真实 UDP 回环），把请求体作为文件做一次回环可靠传输，
// 支持固定种子的丢包/重复/乱序/旧代际报文注入，并返回完整统计。
package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	rt "reliableudp"
)

type server struct{}

func main() {
	addr := flag.String("addr", ":8080", "HTTP 监听地址")
	flag.Parse()

	mux := http.NewServeMux()
	s := server{}
	mux.HandleFunc("/", s.handleIndex)
	mux.HandleFunc("/transfer", s.handleTransfer)

	log.Printf("listening on %s  (POST /transfer)", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func (server) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprint(w, indexText)
}

const indexText = `UDP 可靠传输模拟（纯后端）

POST /transfer            请求体即“文件”原始字节，返回 JSON 报告
GET  /                    本说明

查询参数（均可选）：
  mode=fake|udp           内存链路（默认）或真实 UDP 回环
  seed=12345              故障随机种子（固定种子 => 可复现）
  loss=0.3                数据方向丢包率
  ackloss=0.3             ACK 方向丢包率
  dup=0.1  ackdup=0.1     两个方向的重复率
  reorder=0.1  hold=2     数据方向乱序率与扣留报文数
  budget=20               各类故障的总次数预算（默认 20=有限；0=不限次数）
  mss=1024 window=8       MSS 与滑动窗口大小
  rto=25ms                重传超时（Go duration）
  startseq=0              起始序号（可用大值验证 32 位回绕，如 4294967290）
  gen=42                  本次连接代际（会注入 gen-1 的旧连接幽灵报文）

示例：
  curl -s -X POST --data-binary @/etc/hostname \
    'http://localhost:8080/transfer?mode=fake&seed=42&loss=1&ackloss=1&budget=8'
`

func (server) handleTransfer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	q := r.URL.Query()

	const maxBody = 16 << 20 // 16 MiB
	body, err := readAllLimit(r.Body, maxBody)
	if err != nil {
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
		return
	}

	cfg := rt.Config{
		MSS:        atoiDefault(q.Get("mss"), 1024),
		WindowSize: atoiDefault(q.Get("window"), 8),
		RTO:        durDefault(q.Get("rto"), 25*time.Millisecond),
		MaxRetries: 200,
		StartSeq:   uint32(atoiDefault(q.Get("startseq"), 0)),
		Clock:      rt.RealClock{},
	}
	seed := int64(atoiDefault(q.Get("seed"), 1))
	gen := uint64(atoiDefault(q.Get("gen"), 42))

	budget := atoiDefault(q.Get("budget"), 20)
	budgetArg := budget // 0 在 HTTP 语义里表示“不限”
	if budget == 0 {
		budgetArg = -1
	}
	mkPolicy := func(rate, dup, reo float64, seed int64, ghosts int) rt.FaultPolicy {
		p := rt.FaultPolicy{
			Seed:     seed,
			LossRate: rate, LossBudget: budgetArg,
			DupRate: dup, DupBudget: budgetArg,
			ReorderRate: reo, ReorderBudget: budgetArg, ReorderHold: atoiDefault(q.Get("hold"), 2),
		}
		if ghosts > 0 {
			p.Ghosts = ghostPackets(gen, ghosts)
		}
		return p
	}
	dataFaults := mkPolicy(floatDefault(q.Get("loss"), 0), floatDefault(q.Get("dup"), 0),
		floatDefault(q.Get("reorder"), 0), seed, 3)
	ackFaults := mkPolicy(floatDefault(q.Get("ackloss"), 0), floatDefault(q.Get("ackdup"), 0),
		0, seed^0x9e3779b9, 3)
	faults := rt.PipeFaults{AB: dataFaults, BA: ackFaults}

	mode := q.Get("mode")
	var a, b rt.Link
	if mode == "udp" {
		ea, eb, err := rt.NewUDPLoopbackPair(rt.UDPOptions{
			Clock: rt.RealClock{}, HoldMax: 5 * time.Millisecond, Faults: faults,
		})
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		a, b = ea, eb
	} else {
		a, b = rt.Pipe(faults)
	}

	// fake 模式也使用真实时钟，但 RTO 保持较小即可，内存链路上故障注入是
	// 同步投递的，重传很快完成。
	var sink bytes.Buffer
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	start := time.Now()
	res, terr := rt.RunTransfer(ctx, a, b, gen, cfg, bytes.NewReader(body), &sink)
	elapsed := time.Since(start)

	ok := terr == nil && bytes.Equal(sink.Bytes(), body)
	resp := map[string]any{
		"ok":      ok,
		"mode":    modeOrFake(mode),
		"elapsed": elapsed.String(),
		"error":   errString(terr),
		"params": map[string]any{
			"size": len(body), "mss": cfg.MSS, "window": cfg.WindowSize,
			"rto": cfg.RTO.String(), "startSeq": cfg.StartSeq, "gen": gen, "seed": seed,
		},
		"hashes": map[string]string{
			"sender":   hex.EncodeToString(res.SenderHash),
			"receiver": hex.EncodeToString(res.ReceiverHash),
		},
		"sender":   res.Sender,
		"receiver": res.Receiver,
		"faults": map[string]any{
			"dataDirection": res.Faults.AB,
			"ackDirection":  res.Faults.BA,
		},
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if !ok {
		w.WriteHeader(http.StatusConflict)
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(resp)
}

func modeOrFake(m string) string {
	if m == "udp" {
		return "udp"
	}
	return "fake"
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func ghostPackets(gen uint64, n int) []*rt.Packet {
	var g []*rt.Packet
	for i := 0; i < n; i++ {
		switch i % 3 {
		case 0:
			g = append(g, &rt.Packet{Type: rt.MsgSYN, Gen: gen - 1})
		case 1:
			g = append(g, &rt.Packet{Type: rt.MsgDATA, Gen: gen - 1, Seq: uint32(i), Payload: []byte("ghost")})
		default:
			g = append(g, &rt.Packet{Type: rt.MsgFINACK, Gen: gen - 1, Ack: uint32(i)})
		}
	}
	return g
}

func atoiDefault(s string, d int) int {
	if s == "" {
		return d
	}
	v, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil {
		return d
	}
	return v
}

func floatDefault(s string, d float64) float64 {
	if s == "" {
		return d
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || v < 0 || v > 1 {
		return d
	}
	return v
}

func durDefault(s string, d time.Duration) time.Duration {
	if s == "" {
		return d
	}
	v, err := time.ParseDuration(strings.TrimSpace(s))
	if err != nil || v <= 0 {
		return d
	}
	return v
}

func readAllLimit(r io.Reader, limit int64) ([]byte, error) {
	// 多读 1 字节用于判断是否超限。
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("request body too large (limit %d bytes)", limit)
	}
	return b, nil
}
