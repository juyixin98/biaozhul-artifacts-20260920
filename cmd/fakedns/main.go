// fakedns 是内置静态分区的本地假 DNS 上游（UDP），用于手动联调，不访问公网。
//
// 分区（见 zone 变量）：
//
//	example.com        A     192.0.2.10, 192.0.2.11   TTL 60
//	example.com        AAAA  2001:db8::10            TTL 30
//	www.example.com    A     192.0.2.20              TTL 5
//	zero.example.com   A     192.0.2.30              TTL 0（不被缓存）
//	long.example.com   A     192.0.2.40              TTL 0x7fffffff（最大合法值）
//	nx.example.com     ->    NXDOMAIN                TTL 不涉及
package main

import (
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"dnsproxy/internal/dnsmsg"
	"dnsproxy/internal/fakedns"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:5354", "UDP 监听地址")
	flag.Parse()

	zone := map[string][]fakedns.ZoneRecord{
		"example.com.": {
			{Type: dnsmsg.TypeA, TTL: 60, IP: net.ParseIP("192.0.2.10")},
			{Type: dnsmsg.TypeA, TTL: 60, IP: net.ParseIP("192.0.2.11")},
			{Type: dnsmsg.TypeAAAA, TTL: 30, IP: net.ParseIP("2001:db8::10")},
		},
		"www.example.com.": {
			{Type: dnsmsg.TypeA, TTL: 5, IP: net.ParseIP("192.0.2.20")},
		},
		"zero.example.com.": {
			{Type: dnsmsg.TypeA, TTL: 0, IP: net.ParseIP("192.0.2.30")},
		},
		"long.example.com.": {
			{Type: dnsmsg.TypeA, TTL: 0x7fffffff, IP: net.ParseIP("192.0.2.40")},
		},
	}

	srv, err := fakedns.Start(*listen, fakedns.ZoneHandler(zone))
	if err != nil {
		log.Fatalf("start fake dns: %v", err)
	}
	log.Printf("fake DNS upstream listening on udp %s", srv.Addr())

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Printf("shutting down")
	_ = srv.Close()
}
