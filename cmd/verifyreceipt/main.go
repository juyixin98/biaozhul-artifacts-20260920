// Command verifyreceipt 离线验证晋升/回退收据：重新用收据中的服务端公钥做 Ed25519 验签，
// 并检查 copy_verified_digest == digest、gen_after == gen_before+1。
//
//	go run ./cmd/verifyreceipt -f receipt.json
//	curl -s .../attempts/<id> | jq -c .receipt_json_raw ...   # 也可直接粘 receipt 文件
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	"atomicpromo/internal/receipt"
)

func main() {
	path := flag.String("f", "", "signed receipt json file")
	flag.Parse()
	if *path == "" {
		fmt.Fprintln(os.Stderr, "-f required")
		os.Exit(2)
	}
	raw, err := os.ReadFile(*path)
	must(err)
	var sr receipt.Signed
	must(json.Unmarshal(raw, &sr))
	if err := receipt.VerifySigned(&sr); err != nil {
		fail("signature INVALID: " + err.Error())
	}
	if sr.Receipt.CopyVerified != sr.Receipt.Digest {
		fail(fmt.Sprintf("receipt mismatch: copy_verified %s != digest %s",
			sr.Receipt.CopyVerified, sr.Receipt.Digest))
	}
	if sr.Receipt.GenAfter != sr.Receipt.GenBefore+1 {
		fail("receipt gen sequence invalid")
	}
	fmt.Printf("RECEIPT VALID: kind=%s env=%s gen=%d->%d digest=%s signer=%s\n",
		sr.Receipt.Kind, sr.Receipt.Env, sr.Receipt.GenBefore, sr.Receipt.GenAfter,
		sr.Receipt.Digest, sr.SignerID)
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "RECEIPT INVALID:", msg)
	os.Exit(1)
}

func must(err error) {
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			fail("file not found")
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
