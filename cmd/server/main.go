package main

import (
	"flag"
	"log"
	"net/http"

	"deadline-admission/httpapi"
	"deadline-admission/scheduler"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	flag.Parse()

	clock := scheduler.RealClock{}
	sch := scheduler.New(clock, scheduler.SleepExecutor{Clock: clock}, scheduler.NewEventLog(clock))
	sch.Start()

	srv := httpapi.NewServer(sch)
	log.Printf("deadline-admission server listening on http://%s", *addr)
	log.Fatal(http.ListenAndServe(*addr, srv))
}
