// Command depsolver 是依赖版本约束求解服务的本地入口。
//
// 子命令：
//
//	depsolver serve   --addr :8080               启动本地 JSON HTTP 服务
//	depsolver resolve --request req.json         离线求解并打印 JSON
//	depsolver plan    --request plan.json        生成锁文件与缓存记录（不下载任何东西）
//
// 所有命令均只读取本地文件/内存注册表，不访问网络注册表。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"

	"depsolver/internal/registry"
	"depsolver/internal/service"
	"depsolver/internal/solver"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = cmdServe(os.Args[2:])
	case "resolve":
		err = cmdResolve(os.Args[2:])
	case "plan":
		err = cmdPlan(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "depsolver:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage: depsolver <command> [flags]

commands:
  serve   --addr :8080           start local JSON HTTP service
  resolve --request req.json     offline resolve, print result JSON
  plan    --request plan.json    write lockfile + cache record (no downloads)`)
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address")
	if err := fs.Parse(args); err != nil {
		return err
	}
	h := service.NewHandler()
	h.PlanWriter = service.LocalPlanWriter{}
	mux := http.NewServeMux()
	h.Routes(mux)
	fmt.Fprintf(os.Stderr, "depsolver serving on %s (no network egress; data is request-local)\n", *addr)
	return http.ListenAndServe(*addr, mux)
}

func cmdResolve(args []string) error {
	fs := flag.NewFlagSet("resolve", flag.ContinueOnError)
	path := fs.String("request", "", "path to resolve request JSON")
	prereleases := fs.Bool("prereleases", false, "include prerelease versions (disable the gate)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *path == "" {
		return fmt.Errorf("--request is required")
	}
	var req service.ResolveRequest
	if err := readJSONFile(*path, &req); err != nil {
		return err
	}
	if *prereleases {
		req.IncludePrereleases = true
	}
	res, err := solveOffline(req)
	if err != nil {
		return err
	}
	return printJSON(res)
}

func cmdPlan(args []string) error {
	fs := flag.NewFlagSet("plan", flag.ContinueOnError)
	path := fs.String("request", "", "path to plan request JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *path == "" {
		return fmt.Errorf("--request is required")
	}
	var req service.PlanRequest
	if err := readJSONFile(*path, &req); err != nil {
		return err
	}
	reg, err := registry.Build(req.Registry)
	if err != nil {
		return err
	}
	res, err := solver.Solve(reg, req.Roots, solver.Options{IncludePrereleases: req.IncludePrereleases})
	if err != nil {
		return err
	}
	if !res.Satisfiable {
		return printJSON(map[string]any{"resolve": res})
	}
	lf := service.BuildLockfile(req.Roots, res)
	written, err := service.LocalPlanWriter{}.Write(req, lf)
	if err != nil {
		return err
	}
	return printJSON(map[string]any{"resolve": res, "lockfile": lf, "written": written})
}

func solveOffline(req service.ResolveRequest) (*solver.Result, error) {
	if len(req.Roots) == 0 {
		return nil, fmt.Errorf("roots must contain at least one dependency")
	}
	reg, err := registry.Build(req.Registry)
	if err != nil {
		return nil, err
	}
	return solver.Solve(reg, req.Roots, solver.Options{IncludePrereleases: req.IncludePrereleases})
}

func readJSONFile(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, v); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	return nil
}

func printJSON(v any) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}
