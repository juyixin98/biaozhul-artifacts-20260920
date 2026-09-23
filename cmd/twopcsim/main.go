// Command twopcsim 是两阶段提交恢复模拟器的 JSON 运行接口。
//
// 用法：
//
//	twopcsim -scenario scenario.json [-out report.json]
//
// 从 stdin 读取场景（当 -scenario 为 "-" 或省略时）。
// 退出码：0=运行完成且无部分提交；2=检测到部分提交或协议违规；1=输入/运行错误。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"twopcsim/internal/runner"
)

func main() {
	scenarioPath := flag.String("scenario", "-", "场景 JSON 文件路径（- 表示 stdin）")
	outPath := flag.String("out", "", "报告输出路径（默认 stdout）")
	flag.Parse()

	var in io.Reader = os.Stdin
	if *scenarioPath != "-" {
		f, err := os.Open(*scenarioPath)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		in = f
	}

	var sc runner.Scenario
	dec := json.NewDecoder(in)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&sc); err != nil {
		fatal(fmt.Errorf("解析场景失败: %w", err))
	}

	r, err := runner.New(sc)
	if err != nil {
		fatal(err)
	}
	r.Run()
	rep := r.Report()
	r.Close()

	var out io.Writer = os.Stdout
	if *outPath != "" {
		f, err := os.Create(*outPath)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		out = f
	}
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	if err := enc.Encode(rep); err != nil {
		fatal(err)
	}

	// 退出码反映安全性结论。
	if rep.Verdict == "partial-commit-detected" || len(rep.ProtocolViolations) > 0 {
		os.Exit(2)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "twopcsim:", err)
	os.Exit(1)
}
