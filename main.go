// Command quorumlab 启动一个纯后端的 N 副本读写仲裁模拟服务（内存存储，仅标准库）。
//
// 用法：
//
//	go run . -n 5 -w 3 -r 3 -addr :8080
package main

import (
	"flag"
	"log"
	"net/http"
)

func main() {
	var (
		n    = flag.Int("n", 5, "副本数 N")
		w    = flag.Int("w", 3, "写仲裁 W（收到多少个副本确认才算写成功）")
		r    = flag.Int("r", 3, "读仲裁 R（收到多少个副本响应才算读成功）")
		addr = flag.String("addr", ":8080", "HTTP 监听地址")
	)
	flag.Parse()

	cluster, err := NewCluster(*n, *w, *r)
	if err != nil {
		log.Fatalf("invalid parameters: %v", err)
	}
	srv := NewServer(cluster)
	log.Printf("quorumlab listening on %s (N=%d W=%d R=%d; W+R>N = %v)",
		*addr, *n, *w, *r, *w+*r > *n)
	log.Printf("注意：W+R>N 不自动保证线性一致，详见 README。")
	if err := http.ListenAndServe(*addr, srv.Mux()); err != nil {
		log.Fatal(err)
	}
}
