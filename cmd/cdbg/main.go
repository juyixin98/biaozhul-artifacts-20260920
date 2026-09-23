// Command cdbg 是内容驱动构建图的本地构建工程服务与命令行工具。
//
// 子命令：
//
//	cdbg serve  启动本地 JSON HTTP 服务（默认 127.0.0.1:8787）
//	cdbg run    在进程内执行一次构建（最适合测试夹具与脚本）
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"cdbg/internal/engine"
	"cdbg/internal/graph"
	"cdbg/internal/server"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		if err := runServe(os.Args[2:]); err != nil {
			fatal(err)
		}
	case "run":
		if err := runBuild(os.Args[2:]); err != nil {
			fatal(err)
		}
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func commonFlags(fs *flag.FlagSet) (workDir, cacheDir, stateDir *string) {
	wd, _ := os.Getwd()
	cacheDefault, stateDefault := defaultStoreDirs()
	workDir = fs.String("workdir", wd, "构建工作目录（用户文件与声明输出所在）")
	cacheDir = fs.String("cachedir", cacheDefault, "内容寻址缓存目录（必须在 workdir 之外）")
	stateDir = fs.String("statedir", stateDefault, "构建状态目录（必须在 workdir 之外）")
	return
}

func defaultStoreDirs() (string, string) {
	base := os.Getenv("CDBG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil || home == "" {
			base = filepath.Join(os.TempDir(), "cdbg")
		} else {
			base = filepath.Join(home, ".cdbg")
		}
	}
	return filepath.Join(base, "cache"), filepath.Join(base, "state")
}

func runServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8787", "监听地址（仅建议回环地址）")
	workDir, cacheDir, stateDir := commonFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if isWithin(*cacheDir, *workDir) || isWithin(*stateDir, *workDir) {
		return fmt.Errorf("cachedir/statedir 不能位于 workdir 之内")
	}
	eng, err := engine.New(*cacheDir, *stateDir, envMap(os.Environ()))
	if err != nil {
		return err
	}
	srv := server.New(eng, *workDir, *cacheDir, *stateDir)
	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "cdbg serve: 监听 http://%s  workdir=%s cachedir=%s\n", ln.Addr(), *workDir, *cacheDir)
	httpSrv := &http.Server{Handler: srv.Routes(), ReadHeaderTimeout: 10 * time.Second}
	return httpSrv.Serve(ln)
}

func runBuild(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	specPath := fs.String("spec", "", "构建图 spec JSON 文件路径（必填）")
	workDir, cacheDir, stateDir := commonFlags(fs)
	targets := fs.String("targets", "", "逗号分隔的目标节点；为空表示全部")
	force := fs.Bool("force", false, "忽略缓存查找强制重建（仍会发布缓存）")
	dryRun := fs.Bool("dry-run", false, "只规划并解释命中/失效，不执行任何命令")
	pretty := fs.Bool("pretty", true, "缩进输出 JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *specPath == "" {
		return fmt.Errorf("-spec 必填")
	}
	if isWithin(*cacheDir, *workDir) || isWithin(*stateDir, *workDir) {
		return fmt.Errorf("cachedir/statedir 不能位于 workdir 之内")
	}
	raw, err := os.ReadFile(*specPath)
	if err != nil {
		return fmt.Errorf("读取 spec: %w", err)
	}
	var spec graph.Spec
	if err := json.Unmarshal(raw, &spec); err != nil {
		return fmt.Errorf("解析 spec: %w", err)
	}
	eng, err := engine.New(*cacheDir, *stateDir, envMap(os.Environ()))
	if err != nil {
		return err
	}
	var tlist []string
	if *targets != "" {
		for _, t := range strings.Split(*targets, ",") {
			if t = strings.TrimSpace(t); t != "" {
				tlist = append(tlist, t)
			}
		}
	}
	result, err := eng.Build(*workDir, engine.Options{
		Spec:    &spec,
		Targets: tlist,
		Force:   *force,
		DryRun:  *dryRun,
	})
	if err != nil {
		return err
	}
	var out []byte
	if *pretty {
		out, err = json.MarshalIndent(result, "", "  ")
	} else {
		out, err = json.Marshal(result)
	}
	if err != nil {
		return err
	}
	fmt.Println(string(out))
	if !result.Success {
		os.Exit(1)
	}
	return nil
}

func envMap(environ []string) map[string]string {
	m := map[string]string{}
	for _, kv := range environ {
		if i := strings.IndexByte(kv, '='); i > 0 {
			m[kv[:i]] = kv[i+1:]
		}
	}
	return m
}

func isWithin(child, parent string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	rel = filepath.ToSlash(rel)
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, "../"))
}

func usage() {
	fmt.Fprint(os.Stderr, `cdbg — 内容驱动构建图（纯后端）

用法:
  cdbg serve  [-addr 127.0.0.1:8787] [-workdir DIR] [-cachedir DIR] [-statedir DIR]
  cdbg run    -spec spec.json [-targets a,b] [-force] [-dry-run]
              [-workdir DIR] [-cachedir DIR] [-statedir DIR]

缓存与状态目录必须位于工作目录之外。默认在当前目录旁创建
.cdbg-cache 与 .cdbg-state（若当前目录即 workdir，请显式指定到外部）。
`)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "cdbg:", err)
	os.Exit(1)
}
