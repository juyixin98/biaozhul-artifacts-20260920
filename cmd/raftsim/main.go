// Command raftsim 是 Raft 安全子集模拟器的命令行接口：
//
//	raftdemo -f script.json [-trace 500]
//	echo '{"...":...}' | raftdemo -stdin
//
// 读取一份 JSON 事件脚本，执行确定性模拟，把结果 JSON 打印到 stdout。
// 进程退出码：0 表示全部断言通过（ok=true），1 表示有断言/不变量失败，
// 2 表示输入或配置错误。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"raftdemo/internal/sim"
)

func main() {
	file := flag.String("f", "", "事件脚本 JSON 文件路径")
	stdin := flag.Bool("stdin", false, "从标准输入读取脚本 JSON")
	trace := flag.Int("trace", 0, "在结果中包含最多 N 条运行轨迹（0 表示不输出轨迹）")
	flag.Parse()

	raw, err := readInput(*file, *stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, "读取输入失败:", err)
		os.Exit(2)
	}
	script, err := sim.ParseScript(raw)
	if err != nil {
		fmt.Fprintln(os.Stderr, "脚本无效:", err)
		os.Exit(2)
	}
	if script.Storage == "file" && script.StateDir == "" {
		fmt.Fprintln(os.Stderr, "脚本无效: storage=file 时必须提供 state_dir")
		os.Exit(2)
	}

	result := sim.RunWithTrace(script, *trace)
	out, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, "结果序列化失败:", err)
		os.Exit(2)
	}
	fmt.Println(string(out))

	if !result.OK {
		os.Exit(1)
	}
}

func readInput(path string, useStdin bool) ([]byte, error) {
	if useStdin {
		return io.ReadAll(os.Stdin)
	}
	if path == "" {
		return nil, fmt.Errorf("必须用 -f 指定脚本文件，或用 -stdin 从标准输入读取")
	}
	return os.ReadFile(path)
}
