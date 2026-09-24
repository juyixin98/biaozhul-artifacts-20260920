// Command keygen 生成 Ed25519 密钥（测试/示例工具：所有签名都是真实密码学操作）。
//
//	go run ./cmd/keygen -name tester
//	输出:
//	  tester.pub  = hex 公钥（即 signer_key_id）
//	  tester.key  = hex seed（私钥，妥善保存）
package main

import (
	"crypto/ed25519"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"path/filepath"
)

func main() {
	name := flag.String("name", "signer", "key name")
	dir := flag.String("dir", ".", "output directory")
	flag.Parse()

	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	pubPath := filepath.Join(*dir, *name+".pub")
	keyPath := filepath.Join(*dir, *name+".key")
	if err := os.WriteFile(pubPath, []byte(hex.EncodeToString(pub)+"\n"), 0o644); err != nil {
		fatal(err)
	}
	if err := os.WriteFile(keyPath, []byte(hex.EncodeToString(priv.Seed())+"\n"), 0o600); err != nil {
		fatal(err)
	}
	fmt.Printf("wrote %s (public key id) and %s (private seed)\n", pubPath, keyPath)
}

func fatal(err error) { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
