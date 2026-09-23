// Command snapshotsim 是分片屏障快照（Chandy-Lamport）的确定性离散事件模拟器。
//
// 用法：
//
//	snapshotsim -in request.json [-out result.json]
//	snapshotsim < request.json
//
// 输入：JSON 请求（节点、链路、脚本事件）；输出：JSON 结果（快照、余额、信道统计、事件日志）。
// 不依赖任何真实集群、网络或时钟：所有“时间”都是逻辑 tick，由 seed 决定全部随机性。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"snapshotsim/internal/simulator"
)

func main() {
	in := flag.String("in", "", "请求 JSON 文件路径（缺省读 stdin）")
	out := flag.String("out", "", "结果 JSON 输出路径（缺省写 stdout）")
	flag.Parse()

	var inFile *os.File = os.Stdin
	if *in != "" {
		f, err := os.Open(*in)
		if err != nil {
			fatal("打开输入文件失败: %v", err)
		}
		defer f.Close()
		inFile = f
	}

	var req simulator.Request
	dec := json.NewDecoder(inFile)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		fatal("解析请求 JSON 失败: %v", err)
	}

	resp, err := simulator.Execute(&req)
	if err != nil {
		fatal("模拟失败: %v", err)
	}

	var outFile *os.File = os.Stdout
	if *out != "" {
		f, err := os.Create(*out)
		if err != nil {
			fatal("创建输出文件失败: %v", err)
		}
		defer f.Close()
		outFile = f
	}
	enc := json.NewEncoder(outFile)
	enc.SetIndent("", "  ")
	if err := enc.Encode(resp); err != nil {
		fatal("写出结果失败: %v", err)
	}
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
