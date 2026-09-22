// stubnode 启动一个可注入故障的桩节点 gRPC 服务。
//
// 用法示例:
//
//	stubnode -id node-a -fixture examples/fixture.json -addr 127.0.0.1:50051
//	stubnode -id node-b -fixture examples/fixture.json -corrupt-payload 16:32 -timeout 0:16
//	stubnode -id node-c -fixture examples/fixture.json -corrupt-parent 32:48 -inflate-tip 10
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"nodesync/internal/chain"
	"nodesync/internal/harness"
)

func main() {
	id := flag.String("id", "node", "节点 ID（来源证据用）")
	addr := flag.String("addr", "127.0.0.1:0", "监听地址，:0 表示随机端口")
	fixturePath := flag.String("fixture", "examples/fixture.json", "诚实链夹具路径")
	corruptPayload := flag.String("corrupt-payload", "", "payload 损坏区间，形如 16:32（可逗号分隔多个）")
	corruptParent := flag.String("corrupt-parent", "", "错误父哈希区间，形如 32:48")
	timeouts := flag.String("timeout", "", "超时区间，形如 0:16")
	errors := flag.String("errors", "", "gRPC 错误区间，形如 48:56")
	shorts := flag.String("short", "", "短段区间，形如 8:16")
	inflate := flag.Uint64("inflate-tip", 0, "宣称高度虚高 N")
	invalidTip := flag.Bool("invalid-tip-hash", false, "宣称链尖哈希填 0")
	timeoutMs := flag.Int("timeout-ms", 3000, "超时故障的服务端 sleep 毫秒数")
	flag.Parse()

	fx, err := chain.LoadFixture(*fixturePath)
	if err != nil {
		log.Fatalf("加载夹具失败: %v", err)
	}
	fault := harness.FaultSpec{
		CorruptPayloadRanges: parseRanges(*corruptPayload),
		CorruptParentRanges:  parseRanges(*corruptParent),
		TimeoutRanges:        parseRanges(*timeouts),
		ErrorRanges:          parseRanges(*errors),
		ShortRanges:          parseRanges(*shorts),
		TimeoutDelay:         time.Duration(*timeoutMs) * time.Millisecond,
		AdvertisedTipInflate: *inflate,
		AdvertisedTipInvalid: *invalidTip,
	}

	node, err := harness.NewNodeWithAddr(*id, fx, fault, *addr)
	if err != nil {
		log.Fatalf("创建节点失败: %v", err)
	}
	go node.Serve()

	log.Printf("桩节点 %s 监听 %s（诚实链尖=%d，宣称高度=%d）",
		*id, node.Addr(), fx.ChainTip, fx.ChainTip+*inflate)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	log.Printf("节点 %s 停止", *id)
	node.Stop()
}

// parseRanges 解析 "16:32,48:56" 形式的区间列表。
func parseRanges(s string) []harness.Range {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil
	}
	var out []harness.Range
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		pieces := strings.SplitN(part, ":", 2)
		if len(pieces) != 2 {
			log.Fatalf("区间格式错误 %q，应为 start:end", part)
		}
		start, err := strconv.ParseUint(strings.TrimSpace(pieces[0]), 10, 64)
		if err != nil {
			log.Fatalf("区间起点解析失败 %q: %v", part, err)
		}
		end, err := strconv.ParseUint(strings.TrimSpace(pieces[1]), 10, 64)
		if err != nil {
			log.Fatalf("区间终点解析失败 %q: %v", part, err)
		}
		out = append(out, harness.Range{Start: start, End: end})
	}
	return out
}
