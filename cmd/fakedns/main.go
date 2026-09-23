// Command fakedns runs the locally controlled fake UDP DNS upstream.
//
// It serves a small built-in zone plus the crafted hostile names described in
// package fakeserver. Everything stays on loopback; no public DNS is queried.
package main

import (
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"dnscomp-proxy/internal/dnsmsg"
	"dnscomp-proxy/internal/fakeserver"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:1053", "UDP listen address")
	flag.Parse()

	zone := []fakeserver.RecordConfig{
		{Name: "example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 10).To4(), TTL: 30},
		{Name: "www.example.com", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 20).To4(), TTL: 15},
		{Name: "longttl.example", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 21).To4(), TTL: 600},
		{Name: "v6.example", Type: dnsmsg.TypeAAAA, IP: net.ParseIP("2001:db8::2"), TTL: 45},
		{Name: "multi.example", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 30).To4(), TTL: 20},
		{Name: "multi.example", Type: dnsmsg.TypeA, IP: net.IPv4(192, 0, 2, 31).To4(), TTL: 10},
	}

	srv, err := fakeserver.New(*addr, zone)
	if err != nil {
		log.Fatalf("listen %s: %v", *addr, err)
	}
	log.Printf("fake upstream listening on udp %s (zone: %d records)", srv.LocalAddr(), len(zone))

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		log.Printf("shutting down")
		_ = srv.Close()
	}()

	if err := srv.Serve(); err != nil {
		log.Fatalf("serve: %v", err)
	}
	fmt.Fprintln(os.Stderr, "fake upstream stopped")
}
