// genfixture 生成确定性测试链与可信样例夹具（JSON）。
//
// 用法:
//
//	genfixture -out examples/fixture.json -tip 63 -checkpoints 0,16,32,48,63
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"strings"

	"nodesync/internal/chain"
)

func main() {
	out := flag.String("out", "examples/fixture.json", "夹具输出路径")
	tip := flag.Uint64("tip", 63, "诚实链最高高度")
	cp := flag.String("checkpoints", "0,16,32,48,63", "可信样例检查点高度（逗号分隔）")
	flag.Parse()

	blocks := chain.GenerateChain(*tip)
	checkpoints := []uint64{}
	for _, s := range strings.Split(*cp, ",") {
		var h uint64
		if _, err := fmt.Sscanf(strings.TrimSpace(s), "%d", &h); err != nil {
			log.Fatalf("检查点解析失败 %q: %v", s, err)
		}
		checkpoints = append(checkpoints, h)
	}
	sample := chain.SampleForChain(blocks, checkpoints)
	fx := &chain.Fixture{
		ChainTip:      *tip,
		Blocks:        blocks,
		TrustedSample: sample,
	}
	if err := chain.SaveFixture(*out, fx); err != nil {
		log.Fatalf("写入夹具失败: %v", err)
	}
	fmt.Fprintf(os.Stdout, "已生成夹具: %s（高度 0..%d，%d 个检查点，%d 个区块）\n",
		*out, *tip, len(checkpoints), len(blocks))
}
