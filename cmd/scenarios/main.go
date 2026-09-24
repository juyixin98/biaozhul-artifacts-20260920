// scenarios 在本地离线回放三个验收场景，输出每步决策（含所用样本与理由）。
//
//	go run ./cmd/scenarios                 # 打印三场景的决策表格
//	go run ./cmd/scenarios -json           # 打印完整 JSON（含 usedSamples/理由）
//	go run ./cmd/scenarios -write-dir .    # 写样例请求文件到指定目录
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"offline-scaler/internal/scaler"
)

func main() {
	asJSON := flag.Bool("json", false, "输出完整 JSON")
	writeDir := flag.String("write-dir", "", "将三个场景的回放请求写成 JSON 文件到该目录")
	only := flag.String("only", "", "只运行指定场景: spike | ramp | oscillation")
	flag.Parse()

	var scenarios []scaler.Scenario
	for _, s := range scaler.AllScenarios() {
		if *only != "" && !strings.HasPrefix(s.Name, *only) {
			continue
		}
		scenarios = append(scenarios, s)
	}
	if len(scenarios) == 0 {
		fmt.Fprintln(os.Stderr, "没有匹配的场景（可选 spike/ramp/oscillation）")
		os.Exit(1)
	}

	if *writeDir != "" {
		if err := os.MkdirAll(*writeDir, 0o755); err != nil {
			fatal(err)
		}
	}

	type printed struct {
		scenario scaler.Scenario
		result   *scaler.ReplayResult
	}
	var all []printed

	for _, sc := range scenarios {
		res, err := scaler.Replay(sc.Request)
		if err != nil {
			fatal(err)
		}
		all = append(all, printed{sc, res})

		if *writeDir != "" {
			name := map[string]string{
				"spike":       "examples/spike-request.json",
				"ramp":        "examples/ramp-request.json",
				"oscillation": "examples/oscillation-request.json",
			}[strings.Split(sc.Name, "-")[0]]
			path := *writeDir + "/" + name
			if err := os.MkdirAll(path[:strings.LastIndex(path, "/")], 0o755); err != nil {
				fatal(err)
			}
			writeJSON(path, sc.Request)
			fmt.Fprintf(os.Stderr, "已写样例请求: %s\n", path)
		}
	}

	if *asJSON {
		out := make([]map[string]any, 0, len(all))
		for _, p := range all {
			out = append(out, map[string]any{
				"scenario": p.scenario,
				"summary":  p.result.Summary,
				"decisions": func() []any {
					xs := make([]any, len(p.result.Decisions))
					for i, d := range p.result.Decisions {
						xs[i] = d
					}
					return xs
				}(),
			})
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fatal(err)
		}
		return
	}

	for i, p := range all {
		if i > 0 {
			fmt.Println()
		}
		printTable(p.scenario, p.result)
	}
}

func printTable(sc scaler.Scenario, res *scaler.ReplayResult) {
	fmt.Printf("场景: %s\n说明: %s\n\n", sc.Name, sc.Description)
	fmt.Println("时间   动作       就绪(前→后) 冷启动中 扩容窗口峰值 缩容窗口峰值 信号 缩容窗口完整  理由")
	fmt.Println(strings.Repeat("-", 150))
	for _, d := range res.Decisions {
		act := map[scaler.Action]string{
			scaler.ActionUp:   "扩容",
			scaler.ActionDown: "缩容",
			scaler.ActionHold: "保持",
		}[d.Action]
		sig := "正常"
		if !d.SignalOK {
			sig = "缺失"
		}
		complete := "-"
		if d.TimeSeconds >= sc.Request.Config.DownWindowSeconds {
			complete = boolCN(d.DownWindowComplete)
		}
		upMax, downMax := "-", "-"
		if d.SignalOK {
			upMax = fmt.Sprintf("%.0f", d.UpWindowMaxLoad)
			downMax = fmt.Sprintf("%.0f", d.DownWindowMaxLoad)
		}
		reason := strings.Join(d.Reasons, " / ")
		fmt.Printf("%4ds  %-6s  %4d→%-4d   %4d     %8s   %8s   %s   %s      %s\n",
			d.TimeSeconds, act, d.ReadyBefore, d.ReadyAfter, d.PendingAfter,
			upMax, downMax, sig, complete, reason)
	}
	fmt.Println(strings.Repeat("-", 150))
	fmt.Printf("汇总: 决策 %d 拍，扩容 %d 次，缩容 %d 次，保持 %d 次（其中信号失效保持 %d 次），峰值总副本 %d，最终 就绪=%d 冷启动中=%d\n",
		res.Summary.Ticks, res.Summary.ScaleUps, res.Summary.ScaleDowns,
		res.Summary.Holds, res.Summary.MissingSignal,
		res.Summary.MaxTotalReplicas, res.Summary.FinalReady, res.Summary.FinalPending)
}

func boolCN(b bool) string {
	if b {
		return "是"
	}
	return "否"
}

func writeJSON(path string, v any) {
	f, err := os.Create(path)
	if err != nil {
		fatal(err)
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "错误:", err)
	os.Exit(1)
}
