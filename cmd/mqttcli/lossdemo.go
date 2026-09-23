package main

import (
	"flag"
	"fmt"
	"log"
	"time"

	"mqttsub/internal/mqtt"
)

// runLossDemo drives the acceptance scenarios against one live broker
// using three independent TCP connections (each is a separate session):
//
//  1. ACK loss + reconnect redelivery + DUP: durable subscriber S holds
//     back its PUBACK, drops the TCP connection (crash), reconnects with
//     clean=false, and must receive the same message again with DUP=1 and
//     the SAME packet id. It then PUBACKs; exactly one logical delivery
//     (plus one redelivery) is observed.
//  2. Same packet identifier, different sessions: a second subscriber T
//     subscribes and receives its message on packet id 1 as well — packet
//     ids are allocated per receiving session, never shared.
//
// HTTP POST /publish is used as the message source so the demo has no
// dependency on an external MQTT publisher; the publish call is done with
// net/http in publishViaHTTP.
func runLossDemo(args []string) {
	fs := flag.NewFlagSet("lossdemo", flag.ExitOnError)
	mqttAddr := fs.String("addr", "127.0.0.1:1883", "MQTT broker address")
	httpBase := fs.String("http", "http://127.0.0.1:8081", "HTTP control API base")
	topic := fs.String("topic", "lossdemo/x", "topic to use")
	_ = fs.Parse(args)

	const subA = "lossdemo-subA"
	const subX = "lossdemo-subX"
	const subY = "lossdemo-subY"

	fmt.Println("== scenario 1: PUBACK lost -> reconnect -> DUP=1 redelivery ==")

	// 1.1 Subscriber A connects with a durable session and subscribes.
	a, code, _, err := dial(*mqttAddr, subA, false, 30)
	if err != nil || code != 0 {
		fatalf("A connect: %v code=%d", err, code)
	}
	must(a.subscribe(*topic, 1, 100), "A subscribe")
	fmt.Printf("A(%s): connected (durable), subscribed to %s\n", subA, *topic)

	// 1.2 A message arrives. A receives QoS1 pid and WITHHOLDS the PUBACK.
	publishViaHTTP(*httpBase, *topic, "message-1")
	p1 := mustRecvPublish(a, "A")
	fmt.Printf("A: got #1 DUP=%v pid=%d payload=%q  (PUBACK deliberately withheld)\n",
		p1.Dup, p1.PacketID, string(p1.Payload))
	firstPid := p1.PacketID

	// 1.3 A "crashes": hard TCP close, no DISCONNECT and no PUBACK.
	a.close(false)
	fmt.Println("A: TCP dropped without PUBACK/DISCONNECT (simulated crash)")
	time.Sleep(300 * time.Millisecond)

	// 1.4 A reconnects with the same client id, clean=false: session
	// resumes and the broker redelivers the unacked message with DUP=1.
	a2, code2, sp, err := dial(*mqttAddr, subA, false, 30)
	if err != nil || code2 != 0 {
		fatalf("A reconnect: %v code=%d", err, code2)
	}
	if !sp {
		fatalf("expected sessionPresent=true on durable resume")
	}
	fmt.Printf("A: reconnected, CONNACK sessionPresent=%v\n", sp)

	p2 := mustRecvPublish(a2, "A")
	fmt.Printf("A: got #2 DUP=%v pid=%d payload=%q\n", p2.Dup, p2.PacketID, string(p2.Payload))
	if !p2.Dup {
		fatalf("expected redelivered message to carry DUP=1")
	}
	if p2.PacketID != firstPid {
		fatalf("packet id changed across redelivery: %d -> %d", firstPid, p2.PacketID)
	}
	if string(p2.Payload) != "message-1" {
		fatalf("payload changed across redelivery: %q", p2.Payload)
	}
	// Now acknowledge.
	_, _ = a2.nc.Write(mqtt.EncodePuback(p2.PacketID))
	fmt.Printf("A: PUBACK sent for pid=%d\n", p2.PacketID)
	fmt.Println("A: PASS — at-least-once redelivery with DUP=1 and stable packet id")

	fmt.Println()
	fmt.Println("== scenario 2: same packet id, independent sessions ==")

	// 2.1 Two FRESH durable subscribers on separate TCP connections /
	// sessions. Neither has received anything yet, so each must hand out
	// its own packet id 1 to the same multicast publish. If ids were a
	// global counter, the second session would get 2.
	cliX, codeX, _, err := dial(*mqttAddr, subX, false, 30)
	if err != nil || codeX != 0 {
		fatalf("X connect: %v code=%d", err, codeX)
	}
	must(cliX.subscribe(*topic, 1, 300), "X subscribe")
	fmt.Printf("X(%s): connected (fresh session #1), subscribed\n", subX)

	cliY, codeY, _, err := dial(*mqttAddr, subY, false, 30)
	if err != nil || codeY != 0 {
		fatalf("Y connect: %v code=%d", err, codeY)
	}
	must(cliY.subscribe(*topic, 1, 400), "Y subscribe")
	fmt.Printf("Y(%s): connected (fresh session #2), subscribed\n", subY)

	publishViaHTTP(*httpBase, *topic, "message-2")
	px := mustRecvPublish(cliX, "X")
	py := mustRecvPublish(cliY, "Y")
	fmt.Printf("X: DUP=%v pid=%d payload=%q\n", px.Dup, px.PacketID, string(px.Payload))
	fmt.Printf("Y: DUP=%v pid=%d payload=%q\n", py.Dup, py.PacketID, string(py.Payload))
	if px.Dup || py.Dup {
		fatalf("fresh delivery must not carry DUP=1")
	}
	if px.PacketID != 1 || py.PacketID != 1 {
		fatalf("expected both fresh sessions' first outbound id to be 1, got X=%d Y=%d",
			px.PacketID, py.PacketID)
	}
	fmt.Println("PASS — packet id 1 used concurrently by two different sessions")

	// Cross-check: acknowledging X's pid 1 leaves Y's pid 1 intact.
	// (Verified on the broker side via GET /sessions after the run.)
	cliX.sendRaw(mqtt.EncodePuback(px.PacketID))
	cliY.sendRaw(mqtt.EncodePuback(py.PacketID))
	cliX.close(true)
	cliY.close(true)

	// A (scenario 1) is durable; close it cleanly. It remains on disk
	// until purged with DELETE /sessions/{id}.
	a2.close(true)
	fmt.Println()
	fmt.Println("ALL SCENARIOS PASSED")
	fmt.Println("note: guarantee is at-least-once — duplicates after PUBACK loss")
	fmt.Println("are expected and must be de-duplicated by the application.")
}

func must(err error, what string) {
	if err != nil {
		fatalf("%s: %v", what, err)
	}
}

func fatalf(format string, args ...any) {
	log.Fatalf(format, args...)
}

// recvPublish reads one PUBLISH (skipping keepalives) within a short window.
func mustRecvPublish(c *client, who string) *mqtt.PublishPacket {
	_ = c.nc.SetReadDeadline(time.Now().Add(5 * time.Second))
	for {
		fr, err := mqtt.ReadFrame(c.br)
		if err != nil {
			fatalf("%s: expected PUBLISH, got read error: %v", who, err)
		}
		if fr.Type() == mqtt.TypePUBLISH {
			p, err := mqtt.DecodePublish(fr.Flags(), fr.Body)
			if err != nil {
				fatalf("%s: bad PUBLISH: %v", who, err)
			}
			return p
		}
	}
}
