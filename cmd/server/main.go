// 构建证明验签服务。
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"buildattest/internal/attest"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "监听地址")
	policyPath := flag.String("policy", "", "信任策略 JSON 文件路径（必需）")
	flag.Parse()

	if *policyPath == "" {
		fmt.Fprintln(os.Stderr, "必须指定 -policy <策略文件>")
		os.Exit(2)
	}
	data, err := os.ReadFile(*policyPath)
	if err != nil {
		log.Fatalf("读取策略文件失败: %v", err)
	}
	policy, err := attest.LoadPolicy(data)
	if err != nil {
		log.Fatalf("加载策略失败: %v", err)
	}

	srv := attest.Server(*addr, attest.NewVerifier(policy))
	log.Printf("验签服务监听于 http://%s （策略: %s）", *addr, *policyPath)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("服务退出: %v", err)
	}
}
