// orset 是 OR-Set 确定性离散事件模拟器的命令行入口（纯后端 JSON 接口）。
//
//	orset run <scenario.json> [-o result.json]   运行一个场景，输出结果 JSON
//	orset accept [-o report.json]                运行内置验收枚举，输出报告
//
// 不监听网络端口、不依赖真实集群：节点全在单进程内，所有网络行为由
// 场景参数与 seed 决定，可逐字节复放。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"orset/accept"
	"orset/sim"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "run":
		fs := flag.NewFlagSet("run", flag.ExitOnError)
		out := fs.String("o", "", "将结果 JSON 写入文件（默认写标准输出）")
		_ = fs.Parse(os.Args[2:])
		if fs.NArg() != 1 {
			fmt.Fprintln(os.Stderr, "用法: orset run <scenario.json> [-o result.json]")
			os.Exit(2)
		}
		sc, err := sim.LoadScenario(fs.Arg(0))
		if err != nil {
			fmt.Fprintf(os.Stderr, "加载场景失败: %v\n", err)
			os.Exit(1)
		}
		writeJSON(sim.Run(sc), *out)
	case "accept":
		fs := flag.NewFlagSet("accept", flag.ExitOnError)
		out := fs.String("o", "", "将报告 JSON 写入文件（默认写标准输出）")
		_ = fs.Parse(os.Args[2:])
		rep := accept.RunAll()
		writeJSON(rep, *out)
		if !rep.AllPass {
			os.Exit(1)
		}
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "未知子命令: %s\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `orset — Observed-Remove Set 确定性离散事件模拟器

用法:
  orset run <scenario.json> [-o result.json]  运行场景，输出结果 JSON
  orset accept [-o report.json]               运行内置验收枚举
  orset help                                  显示帮助`)
}

func writeJSON(v interface{}, path string) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "序列化失败: %v\n", err)
		os.Exit(1)
	}
	data = append(data, '\n')
	if path == "" {
		os.Stdout.Write(data)
		return
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "写入 %s 失败: %v\n", path, err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "已写入 %s (%d 字节)\n", path, len(data))
}
