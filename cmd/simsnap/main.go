// Command simsnap 从 JSON 请求运行分片屏障快照模拟。
//
// 用法：
//
//	simsnap -in request.json [-out response.json]
//	cat request.json | simsnap
//
// 不加 -out 时结果写到标准输出。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"simsnap/internal/app"
)

func main() {
	in := flag.String("in", "", "JSON 请求文件路径（缺省读标准输入）")
	out := flag.String("out", "", "JSON 结果输出路径（缺省写标准输出）")
	flag.Parse()

	var r io.Reader = os.Stdin
	if *in != "" {
		f, err := os.Open(*in)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		r = f
	}
	dec := json.NewDecoder(r)
	var req app.Request
	if err := dec.Decode(&req); err != nil {
		fatal(fmt.Errorf("解析请求失败: %w", err))
	}
	if _, err := req.Validate(); err != nil {
		fatal(err)
	}

	resp := app.Run(&req)

	var w io.Writer = os.Stdout
	if *out != "" {
		f, err := os.Create(*out)
		if err != nil {
			fatal(err)
		}
		defer f.Close()
		w = f
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(resp); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "simsnap:", err)
	os.Exit(1)
}
