// Command raftrun 读取一个 Raft 模拟场景 JSON，执行确定性离散事件模拟，
// 把完整结果以 JSON 输出到 stdout；安全断言失败时以非零码退出。
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"raftrun/internal/sim"
)

func main() {
	var (
		scenarioFile = flag.String("scenario", "", "场景 JSON 文件路径（必填）")
		pretty       = flag.Bool("pretty", true, "是否缩进输出 JSON")
	)
	flag.Parse()

	if *scenarioFile == "" {
		fmt.Fprintln(os.Stderr, "usage: raftrun -scenario <file.json> [-pretty=true|false]")
		os.Exit(2)
	}

	data, err := os.ReadFile(*scenarioFile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "read scenario: %v\n", err)
		os.Exit(1)
	}
	var sc sim.Scenario
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&sc); err != nil {
		fmt.Fprintf(os.Stderr, "parse scenario: %v\n", err)
		os.Exit(1)
	}

	res, err := sim.Run(&sc)
	if err != nil {
		fmt.Fprintf(os.Stderr, "simulation: %v\n", err)
		os.Exit(1)
	}

	var out []byte
	if *pretty {
		out, err = json.MarshalIndent(res, "", "  ")
	} else {
		out, err = json.Marshal(res)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal result: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(out))

	if !res.InvariantCheck.Passed {
		os.Exit(3)
	}
}
