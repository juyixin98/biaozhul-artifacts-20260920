package server

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"modbus-receipt/internal/protocol"
	"modbus-receipt/internal/receipt"
	"modbus-receipt/internal/register"
	"modbus-receipt/internal/replay"
)

func startServer(t *testing.T, nReg int, units []byte) (*Server, *register.Store, *receipt.Store, string, context.CancelFunc) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "receipts.db")
	rs, err := receipt.Open(path, []byte("e2e-test-key"))
	if err != nil {
		t.Fatal(err)
	}
	st := register.New(nReg)
	logger := log.New(io.Discard, "", 0)
	srv, err := New(Config{Listen: "127.0.0.1:0", Registers: nReg, AllowedUnits: units, IdleTimeout: 5 * time.Second},
		st, rs, logger)
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Listen(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go srv.Serve(ctx)
	addr := srv.Addr().String()
	t.Cleanup(func() {
		cancel()
		rs.Close()
	})
	return srv, st, rs, addr, cancel
}

func dialRaw(t *testing.T, addr string) net.Conn {
	t.Helper()
	c, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	c.SetDeadline(time.Now().Add(5 * time.Second))
	return c
}

func rawRequest(c net.Conn, adu []byte) (*protocol.ADU, error) {
	if _, err := c.Write(adu); err != nil {
		return nil, err
	}
	return protocol.ReadADU(c)
}

func buildADU(txn uint16, unit byte, pdu []byte) []byte {
	b, err := (&protocol.ADU{TransactionID: txn, ProtocolID: 0, UnitID: unit, PDU: pdu}).Marshal()
	if err != nil {
		panic(err)
	}
	return b
}

func wantException(t *testing.T, resp *protocol.ADU, fc, code byte) {
	t.Helper()
	if resp == nil || len(resp.PDU) < 2 {
		t.Fatalf("no exception response: %+v", resp)
	}
	if resp.PDU[0] != fc|0x80 || resp.PDU[1] != code {
		t.Fatalf("want exception fc=0x%02x code=0x%02x, got % x", fc, code, resp.PDU)
	}
}

func TestNormalWriteThenRead(t *testing.T) {
	_, store, _, addr, _ := startServer(t, 100, []byte{1})
	cc, err := replay.Dial(addr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer cc.Close()
	cc.SetDeadline(3 * time.Second)

	if _, err := cc.WriteRegisters(1, 1, 10, []uint16{0x1111, 0x2222, 0x3333}); err != nil {
		t.Fatal(err)
	}
	vals, _, err := cc.ReadRegisters(2, 1, 9, 4)
	if err != nil {
		t.Fatal(err)
	}
	want := []uint16{0, 0x1111, 0x2222, 0x3333}
	for i := range want {
		if vals[i] != want[i] {
			t.Fatalf("reg %d = %#x want %#x (snapshot %v)", 9+i, vals[i], want[i], vals)
		}
	}
	// Written image visible in-process too.
	got, _ := store.Read(10, 3)
	if got[0] != 0x1111 || got[2] != 0x3333 {
		t.Fatalf("store image wrong: %v", got)
	}
}

func TestExceptionCodes(t *testing.T) {
	_, _, rs, addr, _ := startServer(t, 100, []byte{1})
	c := dialRaw(t, addr)
	defer c.Close()

	// 0x02 ILLEGAL DATA ADDRESS on read crossing the bank.
	resp, err := rawRequest(c, buildADU(1, 1, protocol.ReadRequestPDU(99, 5)))
	if err != nil {
		t.Fatal(err)
	}
	wantException(t, resp, 0x03, 0x02)
	if resp.TransactionID != 1 {
		t.Fatalf("txn not echoed: %d", resp.TransactionID)
	}

	// 0x03 ILLEGAL DATA VALUE on read quantity out of range.
	resp, _ = rawRequest(c, buildADU(2, 1, protocol.ReadRequestPDU(0, 126)))
	wantException(t, resp, 0x03, 0x03)

	// 0x01 ILLEGAL FUNCTION.
	resp, _ = rawRequest(c, buildADU(3, 1, []byte{0x04, 0, 0, 0, 1}))
	wantException(t, resp, 0x04, 0x01)

	// 0x0B gateway-target for unknown unit.
	resp, _ = rawRequest(c, buildADU(4, 9, protocol.ReadRequestPDU(0, 1)))
	wantException(t, resp, 0x03, 0x0B)

	// 0x03 FC10 byte-count/quantity mismatch.
	pdu := protocol.WriteRequestPDU(0, []uint16{1, 2})
	pdu[5] = 2 // byte count says one register
	resp, _ = rawRequest(c, buildADU(5, 1, pdu))
	wantException(t, resp, 0x10, 0x03)

	// 0x02 FC10 range crossing: whole batch rejected.
	resp, _ = rawRequest(c, buildADU(6, 1, protocol.WriteRequestPDU(98, []uint16{7, 7, 7})))
	wantException(t, resp, 0x10, 0x02)

	// Every exception was audited in SQLite.
	events, err := rs.RecentEvents(20)
	if err != nil {
		t.Fatal(err)
	}
	codes := map[byte]int{}
	for _, e := range events {
		codes[e.ExceptionCode]++
	}
	for _, code := range []byte{0x01, 0x02, 0x03, 0x0B} {
		if codes[code] == 0 {
			t.Fatalf("exception 0x%02x not audited, events: %+v", code, events)
		}
	}
}

func TestOutOfRangeWriteChangesNothing(t *testing.T) {
	_, store, _, addr, _ := startServer(t, 10, []byte{1})
	// Put known values near the top.
	cc, _ := replay.Dial(addr, time.Second)
	cc.SetDeadline(3 * time.Second)
	if _, err := cc.WriteRegisters(1, 1, 5, []uint16{50, 60, 70, 80, 90}); err != nil {
		t.Fatal(err)
	}
	cc.Close()

	cc, _ = replay.Dial(addr, time.Second)
	cc.SetDeadline(3 * time.Second)
	defer cc.Close()
	// Write 4 registers from 8 -> crosses bank (8,9 valid; 10,11 not).
	resp, err := cc.Request(2, 1, protocol.WriteRequestPDU(8, []uint16{81, 91, 11, 22}))
	if err != nil {
		t.Fatal(err)
	}
	wantException(t, resp, 0x10, 0x02)

	// Even the in-range portion of the failed batch must be untouched.
	got, _ := store.Read(5, 5)
	for i, want := range []uint16{50, 60, 70, 80, 90} {
		if got[i] != want {
			t.Fatalf("reg %d = %d want %d: failed batch partially applied!", 5+i, got[i], want)
		}
	}
}

func TestFragmentedWriteReassembled(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 100, []byte{1})
	cc, _ := replay.Dial(addr, time.Second)
	defer cc.Close()
	cc.SetDeadline(10 * time.Second)

	values := []uint16{0x0100, 0x0200, 0x0300, 0x0400, 0x0500, 0x0600}
	// One byte per TCP segment with a 2ms gap (~23 syscalls).
	resp, err := cc.WriteRegistersFragmented(9, 1, 20, values, 1, 2*time.Millisecond)
	if err != nil {
		t.Fatalf("fragmented write: %v", err)
	}
	if resp.TransactionID != 9 {
		t.Fatalf("txn echo: %d", resp.TransactionID)
	}
	got, _, err := cc.ReadRegisters(10, 1, 20, 6)
	if err != nil {
		t.Fatal(err)
	}
	for i := range values {
		if got[i] != values[i] {
			t.Fatalf("reg %d = %#x want %#x", 20+i, got[i], values[i])
		}
	}
}

func TestCoalescedStickyPackets(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 100, []byte{1})
	cc, _ := replay.Dial(addr, time.Second)
	defer cc.Close()
	cc.SetDeadline(5 * time.Second)

	// Three ADUs in ONE write: a good write, a read, and an out-of-range
	// read. The server must answer all three, in order, with the txn ids
	// echoed and the correct exception on the third.
	adus := [][]byte{
		buildADU(100, 1, protocol.WriteRequestPDU(0, []uint16{0xAAAA, 0xBBBB})),
		buildADU(101, 1, protocol.ReadRequestPDU(0, 2)),
		buildADU(102, 1, protocol.ReadRequestPDU(500, 1)),
	}
	resps, err := cc.SendCoalesced(adus)
	if err != nil {
		t.Fatal(err)
	}
	if len(resps) != 3 {
		t.Fatalf("got %d responses", len(resps))
	}
	if resps[0].TransactionID != 100 || resps[0].PDU[0] != 0x10 {
		t.Fatalf("resp0 wrong: txn=%d pdu=%x", resps[0].TransactionID, resps[0].PDU)
	}
	if resps[1].TransactionID != 101 {
		t.Fatalf("resp1 txn=%d", resps[1].TransactionID)
	}
	vals, err := protocol.ParseReadResponse(resps[1].PDU)
	if err != nil || vals[0] != 0xAAAA || vals[1] != 0xBBBB {
		t.Fatalf("resp1 values=%v err=%v", vals, err)
	}
	wantException(t, resps[2], 0x03, 0x02)
	if resps[2].TransactionID != 102 {
		t.Fatalf("resp2 txn=%d", resps[2].TransactionID)
	}
}

func TestDuplicateTransactionIDsBothApplied(t *testing.T) {
	_, store, rs, addr, _ := startServer(t, 100, []byte{1})
	cc, _ := replay.Dial(addr, time.Second)
	defer cc.Close()
	cc.SetDeadline(5 * time.Second)

	// Two FC10 requests reusing the same MBAP transaction id 7. Per the
	// Modbus TCP standard, transaction ids are per-connection matching
	// tokens with no dedupe/idempotency: both writes apply, both get
	// receipt rows, both responses echo txn 7.
	v1 := []uint16{0x1111}
	v2 := []uint16{0x2222}
	r1, err := cc.WriteRegisters(7, 1, 0, v1)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := cc.WriteRegisters(7, 1, 0, v2)
	if err != nil {
		t.Fatal(err)
	}
	if r1.TransactionID != 7 || r2.TransactionID != 7 {
		t.Fatalf("txn echoes: %d %d", r1.TransactionID, r2.TransactionID)
	}
	got, _ := store.Read(0, 1)
	if got[0] != 0x2222 {
		t.Fatalf("second duplicate write did not apply: %#x", got[0])
	}
	list, err := rs.RecentReceipts(10)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, r := range list {
		if r.TransactionID == 7 {
			count++
		}
	}
	if count != 2 {
		t.Fatalf("want 2 receipts with txn 7, got %d", count)
	}
}

func TestMidFrameDisconnect(t *testing.T) {
	_, store, _, addr, _ := startServer(t, 100, []byte{1})

	// Case A: FIN after a partial frame (4 of ~21 bytes of FC10).
	cc, _ := replay.Dial(addr, time.Second)
	cc.SetDeadline(3 * time.Second)
	adu := buildADU(1, 1, protocol.WriteRequestPDU(0, []uint16{0xDEAD, 0xBEEF, 0xCAFE}))
	if err := cc.SendAndAbort(adu[:4], "fin"); err != nil {
		t.Fatal(err)
	}

	// Case B: RST after partial MBAP header.
	cc2, _ := replay.Dial(addr, time.Second)
	cc2.SetDeadline(3 * time.Second)
	if err := cc2.SendAndAbort([]byte{0x00, 0x02}, "rst"); err != nil {
		t.Fatal(err)
	}

	// Give the server a moment to reap both connections, then prove the
	// truncated writes never executed and a new connection works.
	time.Sleep(150 * time.Millisecond)
	got, _ := store.Read(0, 3)
	for i, v := range got {
		if v != 0 {
			t.Fatalf("reg %d = %#x: partial-frame write was applied after disconnect", i, v)
		}
	}
	cc3, _ := replay.Dial(addr, time.Second)
	defer cc3.Close()
	cc3.SetDeadline(3 * time.Second)
	if _, _, err := cc3.ReadRegisters(3, 1, 0, 1); err != nil {
		t.Fatalf("server did not survive mid-frame disconnects: %v", err)
	}
}

func TestBadMBAPLengthClosesConnection(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 100, []byte{1})
	c := dialRaw(t, addr)
	defer c.Close()

	// Length = 1 (unit id only, no PDU): unanswerable -> server closes.
	bad := []byte{0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x01}
	if _, err := c.Write(bad); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 16)
	if _, err := c.Read(buf); !errors.Is(err, io.EOF) {
		t.Fatalf("expected connection close on bad length, got err=%v data=%x", err, buf)
	}

	// Wrong protocol id must also cause a close.
	c2 := dialRaw(t, addr)
	defer c2.Close()
	adu := buildADU(1, 1, protocol.ReadRequestPDU(0, 1))
	binary.BigEndian.PutUint16(adu[2:4], 5)
	c2.Write(adu)
	if _, err := c2.Read(make([]byte, 16)); isClosedErr(err) {
		t.Fatalf("expected close on bad protocol id, got %v", err)
	}
}

func isClosedErr(err error) bool {
	return err == nil || (!errors.Is(err, io.EOF) && !strings.Contains(err.Error(), "reset"))
}

// TestConcurrentWritersAndReaders stresses atomicity end-to-end: writers
// place a uniform marker across regs 0..7 while real TCP readers only ever
// accept uniform snapshots.
func TestConcurrentWritersAndReaders(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 64, []byte{1})

	var wg sync.WaitGroup
	var failMu sync.Mutex
	var failures []string
	addFail := func(s string) {
		failMu.Lock()
		failures = append(failures, s)
		failMu.Unlock()
	}

	const writers = 4
	const rounds = 250
	var wgW sync.WaitGroup
	for w := 0; w < writers; w++ {
		wgW.Add(1)
		go func(w int) {
			defer wgW.Done()
			cc, err := replay.Dial(addr, 2*time.Second)
			if err != nil {
				addFail(err.Error())
				return
			}
			defer cc.Close()
			cc.SetDeadline(15 * time.Second)
			for i := 0; i < rounds; i++ {
				marker := uint16(w*rounds + i + 1)
				values := []uint16{marker, marker, marker, marker, marker, marker, marker, marker}
				if _, err := cc.WriteRegisters(uint16(i+1), 1, 0, values); err != nil {
					addFail("write: " + err.Error())
					return
				}
			}
		}(w)
	}

	// Readers only look at regs 0..7, which every write sets as one batch.
	for r := 0; r < 8; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cc, err := replay.Dial(addr, 2*time.Second)
			if err != nil {
				addFail(err.Error())
				return
			}
			defer cc.Close()
			cc.SetDeadline(20 * time.Second)
			var txn uint16 = 50000
			for i := 0; i < 2000; i++ {
				vals, _, err := cc.ReadRegisters(txn, 1, 0, 8)
				txn++
				if err != nil {
					// Writer lock window can make a slow read deadline; be
					// lenient only on timeout, never on torn data.
					if strings.Contains(err.Error(), "timeout") {
						continue
					}
					addFail("read: " + err.Error())
					return
				}
				first := vals[0]
				for _, v := range vals[1:] {
					if v != first {
						addFail(fmt.Sprintf("torn read: %v", vals))
						return
					}
				}
			}
		}()
	}
	wgW.Wait()
	wg.Wait()

	if len(failures) > 0 {
		t.Fatalf("concurrent failures (%d): %v", len(failures), firstN(failures, 5))
	}
}

func firstN(ss []string, n int) []string {
	if len(ss) > n {
		return ss[:n]
	}
	return ss
}

// TestResponseTransactionUnitEcho verifies MBAP fields are echoed on both
// normal and exception responses.
func TestResponseTransactionUnitEcho(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 100, []byte{1, 3})
	c := dialRaw(t, addr)
	defer c.Close()

	cases := []struct {
		txn  uint16
		unit byte
		pdu  []byte
	}{
		{0xFFF0, 3, protocol.ReadRequestPDU(0, 1)},
		{0x0001, 1, protocol.ReadRequestPDU(60000, 1)}, // exception path
		{0xABCD, 1, protocol.WriteRequestPDU(2, []uint16{1})},
	}
	for _, tc := range cases {
		resp, err := rawRequest(c, buildADU(tc.txn, tc.unit, tc.pdu))
		if err != nil {
			t.Fatalf("txn=%x: %v", tc.txn, err)
		}
		if resp.TransactionID != tc.txn || resp.UnitID != tc.unit {
			t.Fatalf("echo mismatch txn=%x unit=%d: got txn=%x unit=%d",
				tc.txn, tc.unit, resp.TransactionID, resp.UnitID)
		}
	}
}

// TestHalfBatchRejectedAtBoundary exercises the exact requirement:
// a write batch where SOME registers are in range still fails entirely.
func TestHalfBatchRejectedAtBoundary(t *testing.T) {
	_, _, _, addr, _ := startServer(t, 100, []byte{1})
	cc, _ := replay.Dial(addr, time.Second)
	defer cc.Close()
	cc.SetDeadline(3 * time.Second)

	// qty=123 from address 50 => ends at 172, bank size 100.
	values := make([]uint16, 123)
	for i := range values {
		values[i] = 0x7777
	}
	resp, err := cc.Request(55, 1, protocol.WriteRequestPDU(50, values))
	if err != nil {
		t.Fatal(err)
	}
	wantException(t, resp, 0x10, 0x02)
}
