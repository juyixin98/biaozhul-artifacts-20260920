// Command licensejudge 提供许可证表达式判定的本地后端：
//
//	licensejudge serve --policy <path> --addr 127.0.0.1:8080 \
//	    --work-dir ./.work --cache-dir ./.cache
//	licensejudge eval  --policy <path> 'MIT AND (Apache-2.0 OR GPL-3.0-only)'
//
// 仅本地运行，不连接任何云平台。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"

	"licensejudge/internal/cache"
	"licensejudge/internal/judge"
	"licensejudge/internal/policy"
	"licensejudge/internal/server"
)

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetPrefix("[licensejudge] ")

	if len(os.Args) < 2 {
		usage(os.Stderr)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		os.Exit(runServe(os.Args[2:]))
	case "eval":
		os.Exit(runEval(os.Args[2:]))
	case "-h", "--help", "help":
		usage(os.Stdout)
		os.Exit(0)
	default:
		fmt.Fprintf(os.Stderr, "未知子命令 %q\n\n", os.Args[1])
		usage(os.Stderr)
		os.Exit(2)
	}
}

func usage(w io.Writer) {
	fmt.Fprint(w, `licensejudge — SPDX 风格许可证表达式判定（纯后端，本地运行）

用法:
  licensejudge serve --policy <文件或目录> [选项]
      --addr       监听地址（默认 127.0.0.1:8080，仅本机）
      --work-dir   作业与审计日志目录（默认 ./.work）
      --cache-dir  AST 磁盘缓存目录（默认 ./.cache，必须与 work-dir 分离）
  licensejudge eval --policy <文件或目录> "<表达式>"
      退出码: 0=allow  1=deny  3=unknown  2=用法/解析错误

判定仅依据所给策略配置，不构成法律意见。
`)
}

func runServe(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	policyPath := fs.String("policy", "configs", "策略 JSON 文件或仅含一个 JSON 的目录")
	addr := fs.String("addr", "127.0.0.1:8080", "监听地址（默认仅本机）")
	workDir := fs.String("work-dir", "./.work", "工作目录（作业与审计日志）")
	cacheDir := fs.String("cache-dir", "./.cache", "AST 缓存目录（必须与工作目录分离）")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	pol, err := policy.LoadPath(*policyPath)
	if err != nil {
		log.Printf("加载策略失败: %v", err)
		return 2
	}
	astCache, err := cache.New(*cacheDir)
	if err != nil {
		log.Print(err)
		return 2
	}
	srv, err := server.New(server.Config{
		Policy:  pol,
		Cache:   astCache,
		WorkDir: *workDir,
	})
	if err != nil {
		log.Print(err)
		return 2
	}
	defer srv.Close()

	log.Printf("策略 %q 已加载；工作目录=%s；缓存目录=%s", pol.Name, mustAbs(*workDir), astCache.Dir())
	log.Printf("监听 http://%s （仅本地，不连接云平台）", *addr)
	httpServer := &http.Server{Addr: *addr, Handler: srv.Routes()}
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Printf("HTTP 服务退出: %v", err)
		return 1
	}
	return 0
}

func runEval(args []string) int {
	fs := flag.NewFlagSet("eval", flag.ContinueOnError)
	policyPath := fs.String("policy", "configs", "策略 JSON 文件或仅含一个 JSON 的目录")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "eval 需要且仅需要一个表达式参数")
		return 2
	}
	pol, err := policy.LoadPath(*policyPath)
	if err != nil {
		log.Printf("加载策略失败: %v", err)
		return 2
	}
	result, err := judge.NewEngine(pol).EvalText(fs.Arg(0))
	if err != nil {
		fmt.Fprintf(os.Stderr, "表达式错误: %v\n", err)
		return 2
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(result); err != nil {
		log.Print(err)
		return 2
	}
	switch result.Verdict {
	case judge.VerdictAllow:
		return 0
	case judge.VerdictDeny:
		return 1
	case judge.VerdictUnknown:
		return 3
	default:
		return 2
	}
}

func mustAbs(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	return abs
}
