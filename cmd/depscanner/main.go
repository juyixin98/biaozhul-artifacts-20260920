// depscanner 是增量 C-include 依赖扫描服务的命令行入口。
//
// 子命令：
//
//	depscanner serve    --addr :8080                    启动 JSON HTTP 服务
//	depscanner scan     --root DIR --targets a.c [..]   执行扫描并写入基线
//	depscanner affected --root DIR --targets a.c [..]   对比基线计算受影响目标
//	depscanner parse    --root DIR --file path          仅解析单文件 include
//
// --quote-dirs / --system-dirs 可重复传入；--cache-dir 必须位于 root 之外。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"

	"depscanner/internal/service"
)

// stringList 支持重复传入的字符串 flag。
type stringList []string

func (s *stringList) String() string { return strings.Join(*s, ",") }
func (s *stringList) Set(v string) error {
	*s = append(*s, v)
	return nil
}

type commonFlags struct {
	root       string
	cacheDir   string
	targets    stringList
	quoteDirs  stringList
	systemDirs stringList
}

func bindCommon(fs *flag.FlagSet) *commonFlags {
	c := &commonFlags{}
	fs.StringVar(&c.root, "root", "", "project root directory")
	fs.StringVar(&c.cacheDir, "cache-dir", "", "cache directory (must be outside root)")
	fs.Var(&c.targets, "targets", "scan target relative to root (repeatable)")
	fs.Var(&c.quoteDirs, "quote-dirs", "extra search dir for quoted includes (repeatable)")
	fs.Var(&c.systemDirs, "system-dirs", "search dir for angled includes (repeatable)")
	return c
}

func (c *commonFlags) scanRequest() service.ScanRequest {
	return service.ScanRequest{
		Root:              c.root,
		Targets:           []string(c.targets),
		QuoteIncludeDirs:  []string(c.quoteDirs),
		SystemIncludeDirs: []string(c.systemDirs),
		CacheDir:          c.cacheDir,
	}
}

func printJSON(v any) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "depscanner:", err)
	os.Exit(1)
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		cmdServe(os.Args[2:])
	case "scan":
		cmdScan(os.Args[2:])
	case "affected":
		cmdAffected(os.Args[2:])
	case "parse":
		cmdParse(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `depscanner — incremental C include dependency scanner

usage:
  depscanner serve     --addr :8080
  depscanner scan      --root DIR --cache-dir DIR --targets rel/path.c [flags]
  depscanner affected  --root DIR --cache-dir DIR --targets rel/path.c [--update-baseline]
  depscanner parse     --root DIR --file rel/path.c

flags:
  --quote-dirs DIR    extra search dir for #include "..." (repeatable)
  --system-dirs DIR   search dir for #include <...> (repeatable)
`)
}

func cmdServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address")
	if err := fs.Parse(args); err != nil {
		os.Exit(2)
	}
	mux := service.Handler()
	fmt.Fprintf(os.Stderr, "depscanner listening on http://%s\n", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		fatal(err)
	}
}

func cmdScan(args []string) {
	fs := flag.NewFlagSet("scan", flag.ContinueOnError)
	c := bindCommon(fs)
	if err := fs.Parse(args); err != nil {
		os.Exit(2)
	}
	resp, err := service.Engine{}.Scan(c.scanRequest())
	if err != nil {
		fatal(err)
	}
	if err := printJSON(resp); err != nil {
		fatal(err)
	}
	if resp.Status == "error" {
		os.Exit(1)
	}
}

func cmdAffected(args []string) {
	fs := flag.NewFlagSet("affected", flag.ContinueOnError)
	c := bindCommon(fs)
	update := fs.Bool("update-baseline", false, "refresh the baseline after analysis")
	if err := fs.Parse(args); err != nil {
		os.Exit(2)
	}
	resp, err := service.Engine{}.Affected(service.AffectedRequest{
		ScanRequest:    c.scanRequest(),
		UpdateBaseline: *update,
	})
	if err != nil {
		fatal(err)
	}
	if err := printJSON(resp); err != nil {
		fatal(err)
	}
	if resp.Status == "error" {
		os.Exit(1)
	}
}

func cmdParse(args []string) {
	fs := flag.NewFlagSet("parse", flag.ContinueOnError)
	root := fs.String("root", "", "project root directory")
	file := fs.String("file", "", "file to parse, relative to root (or absolute)")
	if err := fs.Parse(args); err != nil {
		os.Exit(2)
	}
	resp, err := service.Engine{}.ParseFile(service.ParseRequest{Root: *root, File: *file})
	if err != nil {
		fatal(err)
	}
	if err := printJSON(resp); err != nil {
		fatal(err)
	}
	if resp.Status == "error" {
		os.Exit(1)
	}
}
