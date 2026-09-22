// syncer 是节点区间同步器的客户端/执行器：连接多个桩节点，
// 从 SQLite 检查点续传同步，输出最终链摘要与来源证据报告。
//
// 用法:
//
//	syncer -fixture examples/fixture.json -db run/sync.db \
//	  -peer node-a=127.0.0.1:50051 -peer node-b=127.0.0.1:50052 -peer node-c=127.0.0.1:50053
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"nodesync/internal/chain"
	"nodesync/internal/harness"
	"nodesync/internal/storage"
	"nodesync/internal/syncer"
)

type peerList []string

func (p *peerList) String() string     { return strings.Join(*p, ",") }
func (p *peerList) Set(v string) error { *p = append(*p, v); return nil }

func main() {
	var peers peerList
	fixturePath := flag.String("fixture", "examples/fixture.json", "夹具路径（提供可信样例）")
	dbPath := flag.String("db", "run/sync.db", "SQLite 检查点数据库路径（重启续传）")
	flag.Var(&peers, "peer", "远端节点，格式 id=host:port（可重复）")
	segSize := flag.Uint64("segment-size", syncer.DefaultSegmentSize, "每段区块数")
	parallel := flag.Int("parallel", syncer.DefaultMaxParallel, "并行拉取上限")
	window := flag.Int("window", syncer.DefaultWindowSegs, "乱序缓存段数上限（有界）")
	retries := flag.Int("retries", syncer.DefaultMaxRetries, "单段跨源尝试次数")
	reportPath := flag.String("report", "run/report.json", "JSON 报告输出路径（空=不写文件）")
	cancelAfter := flag.Duration("cancel-after", 0, "非0时运行该时长后取消（演示取消竞争）")
	rpcTimeout := flag.Duration("rpc-timeout", 1500*time.Millisecond, "单次拉取超时")
	flag.Parse()

	if len(peers) == 0 {
		log.Fatal("至少需要一个 -peer id=host:port")
	}

	fx, err := chain.LoadFixture(*fixturePath)
	if err != nil {
		log.Fatalf("加载夹具失败: %v", err)
	}

	store, err := storage.Open(*dbPath)
	if err != nil {
		log.Fatalf("打开数据库失败: %v", err)
	}
	defer store.Close()

	var conns []*harness.Peer
	for _, spec := range peers {
		id, addr, ok := strings.Cut(spec, "=")
		if !ok {
			log.Fatalf("-peer 格式错误: %q，应为 id=host:port", spec)
		}
		p, err := harness.Dial(strings.TrimSpace(id), strings.TrimSpace(addr))
		if err != nil {
			log.Fatalf("连接节点 %s 失败: %v", spec, err)
		}
		defer p.Close()
		conns = append(conns, p)
		log.Printf("已连接节点 %s (%s)", p.ID, p.Addr)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Ctrl-C 或 -cancel-after 触发取消。
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	if *cancelAfter > 0 {
		go func() {
			time.Sleep(*cancelAfter)
			log.Printf("=== 达到 cancel-after=%s，取消同步（在途旧结果必须被丢弃）===", *cancelAfter)
			cancel()
		}()
	}
	go func() {
		<-sigCh
		log.Printf("收到中断信号，取消同步")
		cancel()
	}()

	cfg := syncer.Config{
		Peers: conns, Store: store, Sample: fx.TrustedSample,
		SegmentSize: *segSize, MaxParallel: *parallel,
		WindowSegs: *window, MaxSegRetries: *retries,
		RPCTimeout: *rpcTimeout,
	}
	summary, runErr := syncer.Run(ctx, cfg)

	report, err := syncer.BuildReport(store, fx.TrustedSample, summary)
	if err != nil {
		log.Fatalf("构建报告失败: %v", err)
	}
	fmt.Println(report.PrintText())

	if *reportPath != "" {
		data, _ := report.JSON()
		if dir := filepath.Dir(*reportPath); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				log.Printf("创建报告目录失败: %v", err)
			}
		}
		if err := os.WriteFile(*reportPath, data, 0o644); err != nil {
			log.Printf("写报告失败: %v", err)
		} else {
			log.Printf("JSON 报告已写入 %s", *reportPath)
		}
	}

	if runErr != nil {
		eb, _ := json.Marshal(runErr.Error())
		fmt.Printf("同步未完全成功（如实报告）: %s\n", string(eb))
		// 部分成功（已提交连续前缀）以退出码 2 区分；硬错误为 1。
		if summary != nil && summary.VerifiedTip > 0 {
			os.Exit(2)
		}
		os.Exit(1)
	}
	if summary.Cancelled || !summary.ReachedTarget {
		fmt.Printf("同步在到达目标前结束（取消=%v，已验证检查点=%d，目标=%d）；连续前缀已持久化，可重跑续传\n",
			summary.Cancelled, report.VerifiedTip, summary.TargetTip)
		os.Exit(2)
	}
	if !report.TipMatchesSample {
		log.Fatal("最终链尖哈希与可信样例不一致")
	}
	log.Printf("成功：已验证链尖 %d 与可信样例一致", report.VerifiedTip)
}
