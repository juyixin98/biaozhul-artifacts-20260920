package server

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"modbus-receipt/internal/client"
	"modbus-receipt/internal/protocol"
	"modbus-receipt/internal/storage"
)

type testEnv struct {
	t      *testing.T
	store  *storage.Store
	srv    *Server
	addr   string
	cancel context.CancelFunc
}

func startServer(t *testing.T, bank int) *testEnv {
	t.Helper()
	dir := t.TempDir()
	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN:          "file:" + filepath.Join(dir, "srv.db") + "?_txlock=immediate",
		BankSize:     bank,
		AllowedUnits: []byte{1},
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := New(Config{
		ListenAddr: "127.0.0.1:0",
		Store:      store,
	}, log.New(io.Discard, "", 0))
	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = srv.Serve(ctx) }()
	// Wait for the listener.
	deadline := time.Now().Add(2 * time.Second)
	for srv.Addr() == nil && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if srv.Addr() == nil {
		cancel()
		t.Fatal("server never started listening")
	}
	t.Cleanup(func() {
		srv.Shutdown()
		cancel()
		store.Close()
	})
	return &testEnv{t: t, store: store, srv: srv,
		addr: srv.Addr().String(), cancel: cancel}
}

func (e *testEnv) dial() *client.Client {
	e.t.Helper()
	c, err := client.Dial(e.addr, 2*time.Second)
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { c.Close() })
	return c
}

// Happy path: write a batch, read it back, write again (overwrite).
func TestEndToEnd_WriteReadOverwrite(t *testing.T) {
	env := startServer(t, 100)
	c := env.dial()

	if _, qty, _, err := c.WriteMultipleRegisters(1, 1, 10, []uint16{0x1111, 0x2222}); err != nil {
		t.Fatal(err)
	} else if qty != 2 {
		t.Fatalf("echo qty %d", qty)
	}
	values, _, err := c.ReadHoldingRegisters(2, 1, 9, 4)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(values) != "[0 4369 8738 0]" {
		t.Fatalf("read back %v", values)
	}

	// Overwrite + extend in a second batch.
	if _, _, _, err := c.WriteMultipleRegisters(3, 1, 11, []uint16{7, 8, 9}); err != nil {
		t.Fatal(err)
	}
	values, _, err = c.ReadHoldingRegisters(4, 1, 10, 4)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(values) != "[4369 7 8 9]" {
		t.Fatalf("after overwrite %v", values)
	}
}

// Exception 0x01 for an unsupported function code.
func TestException_IllegalFunction(t *testing.T) {
	env := startServer(t, 100)
	raw, err := net.Dial("tcp", env.addr)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	// FC04 (Read Input Registers) is NOT in the explicitly supported set.
	req := protocol.Frame{TransactionID: 5, UnitID: 1,
		PDU: []byte{0x04, 0x00, 0x00, 0x00, 0x01}}.Encode()
	if _, err := raw.Write(req); err != nil {
		t.Fatal(err)
	}
	resp, err := protocol.ReadFrame(raw)
	if err != nil {
		t.Fatal(err)
	}
	if resp.TransactionID != 5 {
		t.Fatalf("txn echo %d", resp.TransactionID)
	}
	code, isExc := protocol.AsException(resp.PDU)
	if !isExc || code != protocol.ExcIllegalFunction {
		t.Fatalf("want exc 0x01, got % X", resp.PDU)
	}
}

// Exception 0x02: reading and writing past the bank.
func TestException_IllegalDataAddress(t *testing.T) {
	env := startServer(t, 16)
	c := env.dial()

	if _, _, err := c.ReadHoldingRegisters(1, 1, 15, 2); err == nil {
		t.Fatal("read past bank must fail")
	} else {
		var ee *client.ExceptionError
		if !errors.As(err, &ee) || ee.ExceptionCode != protocol.ExcIllegalDataAddress {
			t.Fatalf("want 0x02, got %v", err)
		}
	}
	if _, _, _, err := c.WriteMultipleRegisters(2, 1, 15, []uint16{1, 2}); err == nil {
		t.Fatal("write past bank must fail")
	} else {
		var ee *client.ExceptionError
		if !errors.As(err, &ee) || ee.ExceptionCode != protocol.ExcIllegalDataAddress {
			t.Fatalf("want 0x02, got %v", err)
		}
	}
	// No registers of the failed batch may land.
	values, _, err := c.ReadHoldingRegisters(3, 1, 15, 1)
	if err != nil {
		t.Fatal(err)
	}
	if values[0] != 0 {
		t.Fatalf("failed write partially applied: %v", values)
	}
}

// Exception 0x03: structurally illegal PDU (FC10 qty/byte-count mismatch).
func TestException_IllegalDataValue(t *testing.T) {
	env := startServer(t, 100)
	raw, err := net.Dial("tcp", env.addr)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	// FC10, start 0, qty 2 but byte count 6 (says 3 registers).
	pdu, _ := hex.DecodeString("100000000206000100020003")
	req := protocol.Frame{TransactionID: 9, UnitID: 1, PDU: pdu}.Encode()
	if _, err := raw.Write(req); err != nil {
		t.Fatal(err)
	}
	resp, err := protocol.ReadFrame(raw)
	if err != nil {
		t.Fatal(err)
	}
	code, isExc := protocol.AsException(resp.PDU)
	if !isExc || code != protocol.ExcIllegalDataValue {
		t.Fatalf("want exc 0x03, got % X", resp.PDU)
	}
}

// Unknown unit id -> exception 0x0B.
func TestException_UnknownUnit(t *testing.T) {
	env := startServer(t, 100)
	c := env.dial()
	_, _, err := c.ReadHoldingRegisters(1, 7, 0, 1) // only unit 1 configured
	var ee *client.ExceptionError
	if !errors.As(err, &ee) || ee.ExceptionCode != protocol.ExcGatewayPathUnavailable {
		t.Fatalf("want exc 0x0B, got %v", err)
	}
}

// Repeated Transaction IDs are legal and are NOT deduped: two FC10s with the
// same txn both execute and produce two receipts.
func TestDuplicateTransactionIDs_BothExecuted(t *testing.T) {
	env := startServer(t, 100)
	c := env.dial()

	if _, _, _, err := c.WriteMultipleRegisters(42, 1, 0, []uint16{11}); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := c.WriteMultipleRegisters(42, 1, 0, []uint16{22}); err != nil {
		t.Fatal(err)
	}
	values, _, err := c.ReadHoldingRegisters(43, 1, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	if values[0] != 22 {
		t.Fatalf("second duplicate-txn write did not execute: %v", values)
	}
	rs, err := env.store.ListReceipts(context.Background(), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(rs) != 2 || rs[0].TransactionID != 42 || rs[1].TransactionID != 42 {
		t.Fatalf("expected 2 receipts both txn 42, got %+v", rs)
	}
}

// A response must echo the request's exact Transaction ID for every frame,
// including out-of-order hand-chosen IDs on one connection.
func TestTransactionIDEcho(t *testing.T) {
	env := startServer(t, 100)
	raw, err := net.Dial("tcp", env.addr)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	for _, txn := range []uint16{0xFFFF, 7, 0, 12345} {
		req := protocol.Frame{TransactionID: txn, UnitID: 1,
			PDU: protocol.ReadHoldingRegistersRequest{StartAddress: 0, Quantity: 1}.Encode()}.Encode()
		if _, err := raw.Write(req); err != nil {
			t.Fatal(err)
		}
		resp, err := protocol.ReadFrame(raw)
		if err != nil {
			t.Fatal(err)
		}
		if resp.TransactionID != txn {
			t.Fatalf("txn %d echoed as %d", txn, resp.TransactionID)
		}
	}
}

// Client drops the connection AFTER sending a complete FC10 but BEFORE
// reading the response: the write is committed; a fresh connection sees it.
// (Honest Modbus/TCP semantics — no cross-connection rollback/idempotency.)
func TestDisconnectAfterRequest_WriteStillCommitted(t *testing.T) {
	env := startServer(t, 100)
	c1 := env.dial()
	if _, _, _, err := c1.WriteMultipleRegisters(1, 1, 20, []uint16{0xBEEF}); err != nil {
		t.Fatal(err)
	}
	// Second connection: send full request, close immediately without read.
	c2 := client.New(mustDial(t, env.addr))
	pdu := protocol.WriteMultipleRegistersRequest{
		StartAddress: 21, Values: []uint16{0xCAFE}}.Encode()
	req := protocol.Frame{TransactionID: 2, UnitID: 1, PDU: pdu}.Encode()
	if err := c2.SendRawBytes(req); err != nil {
		t.Fatal(err)
	}
	if err := c2.Close(); err != nil {
		t.Fatal(err)
	}

	// Give the server a moment to process the bytes before closing.
	waitForValue(t, env, 21, 0xCAFE)

	c3 := env.dial()
	values, _, err := c3.ReadHoldingRegisters(3, 1, 20, 2)
	if err != nil {
		t.Fatal(err)
	}
	if values[0] != 0xBEEF || values[1] != 0xCAFE {
		t.Fatalf("write before disconnect not committed: %v", values)
	}
}

// Client disconnects MID-FRAME (partial ADU): server closes the connection;
// nothing is applied; a new connection works normally.
func TestDisconnectMidFrame_NothingApplied(t *testing.T) {
	env := startServer(t, 100)
	conn := mustDial(t, env.addr)
	partial, _ := hex.DecodeString("00010000000d0110000a0003060001") // 8 of 20 bytes
	if _, err := conn.Write(partial); err != nil {
		t.Fatal(err)
	}
	conn.Close()

	c := env.dial()
	values, _, err := c.ReadHoldingRegisters(2, 1, 10, 3)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(values) != "[0 0 0]" {
		t.Fatalf("partial frame changed registers: %v", values)
	}
}

// Two complete ADUs sent in ONE write ("粘包") both execute in order and
// yield two parseable responses.
func TestStickyPacket_TwoWritesOneSegment(t *testing.T) {
	env := startServer(t, 100)
	conn := mustDial(t, env.addr)
	defer conn.Close()
	f1 := protocol.Frame{TransactionID: 1, UnitID: 1, PDU: protocol.
		WriteMultipleRegistersRequest{StartAddress: 0, Values: []uint16{1, 2}}.Encode()}.Encode()
	f2 := protocol.Frame{TransactionID: 2, UnitID: 1, PDU: protocol.
		WriteMultipleRegistersRequest{StartAddress: 2, Values: []uint16{3, 4}}.Encode()}.Encode()
	both := append(f1, f2...)
	if _, err := conn.Write(both); err != nil {
		t.Fatal(err)
	}
	r1, err := protocol.ReadFrame(conn)
	if err != nil {
		t.Fatal(err)
	}
	r2, err := protocol.ReadFrame(conn)
	if err != nil {
		t.Fatal(err)
	}
	if r1.TransactionID != 1 || r2.TransactionID != 2 {
		t.Fatalf("response order/ids: %d %d", r1.TransactionID, r2.TransactionID)
	}
	c := client.New(conn)
	values, _, err := c.ReadHoldingRegisters(3, 1, 0, 4)
	if err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(values) != "[1 2 3 4]" {
		t.Fatalf("sticky batch result %v", values)
	}
}

// One FC10 frame split across many tiny writes ("分包").
func TestFragmentedWrite(t *testing.T) {
	env := startServer(t, 100)
	conn := mustDial(t, env.addr)
	defer conn.Close()
	frame := protocol.Frame{TransactionID: 8, UnitID: 1, PDU: protocol.
		WriteMultipleRegistersRequest{StartAddress: 30, Values: []uint16{5, 6, 7, 8}}.Encode()}.Encode()
	for _, b := range frame {
		if _, err := conn.Write([]byte{b}); err != nil {
			t.Fatal(err)
		}
	}
	resp, err := protocol.ReadFrame(conn)
	if err != nil {
		t.Fatal(err)
	}
	if code, exc := protocol.AsException(resp.PDU); exc {
		t.Fatalf("fragmented request rejected: exc 0x%02X", code)
	}
}

// Concurrent clients hammer the bank; at every observable instant reads must
// return either an old batch or a complete new batch, never a mixture.
func TestConcurrentClients_NoTornReads(t *testing.T) {
	env := startServer(t, 1000)

	const writers = 8
	const batches = 20
	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			c := env.dial()
			for k := 0; k < batches; k++ {
				// writer w owns addresses [w*10, w*10+4): a distinct bank so
				// each batch is an all-or-nothing 4-register constant.
				values := []uint16{
					uint16(0xA000 | id<<8 | k),
					uint16(0xA000 | id<<8 | k),
					uint16(0xA000 | id<<8 | k),
					uint16(0xA000 | id<<8 | k),
				}
				if _, _, _, err := c.WriteMultipleRegisters(uint16(k+1), 1,
					uint16(id*10), values); err != nil {
					t.Errorf("write: %v", err)
					return
				}
			}
		}(w)
	}

	// Readers: any 4-register window aligned to a writer must be constant
	// (all zero pre-write, or all equal post-write).
	stop := make(chan struct{})
	var readerWG sync.WaitGroup
	for r := 0; r < 4; r++ {
		readerWG.Add(1)
		go func() {
			defer readerWG.Done()
			c := env.dial()
			for {
				select {
				case <-stop:
					return
				default:
				}
				for w := 0; w < writers; w++ {
					v, _, err := c.ReadHoldingRegisters(0, 1, uint16(w*10), 4)
					if err != nil {
						return
					}
					if !(v[0] == v[1] && v[1] == v[2] && v[2] == v[3]) {
						t.Errorf("torn read at writer bank %d: %v", w, v)
						return
					}
				}
			}
		}()
	}

	wg.Wait()
	close(stop)
	readerWG.Wait()

	// Final state sanity.
	c := env.dial()
	for w := 0; w < writers; w++ {
		v, _, err := c.ReadHoldingRegisters(uint16(100+w), 1, uint16(w*10), 4)
		if err != nil {
			t.Fatal(err)
		}
		want := uint16(0xA000 | w<<8 | (batches - 1))
		for i, got := range v {
			if got != want {
				t.Fatalf("writer %d final[%d]=%04X want %04X", w, i, got, want)
			}
		}
	}
}

// Protocol ID != 0: server rejects with 0x03.
func TestWrongProtocolID(t *testing.T) {
	env := startServer(t, 100)
	conn := mustDial(t, env.addr)
	defer conn.Close()
	req := protocol.Frame{TransactionID: 1, ProtocolID: 0x1234, UnitID: 1,
		PDU: protocol.ReadHoldingRegistersRequest{StartAddress: 0, Quantity: 1}.Encode()}.Encode()
	if _, err := conn.Write(req); err != nil {
		t.Fatal(err)
	}
	resp, err := protocol.ReadFrame(conn)
	if err != nil {
		t.Fatal(err)
	}
	code, isExc := protocol.AsException(resp.PDU)
	if !isExc || code != protocol.ExcIllegalDataValue {
		t.Fatalf("want 0x03 for bad protocol id, got % X", resp.PDU)
	}
}

// Shutdown must return promptly even when an idle keep-alive connection is
// blocked inside ReadFrame (regression: earlier Shutdown waited forever).
func TestShutdownClosesIdleConnections(t *testing.T) {
	dir := t.TempDir()
	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN:          "file:" + filepath.Join(dir, "shut.db") + "?_txlock=immediate",
		BankSize:     8,
		AllowedUnits: []byte{1},
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := New(Config{ListenAddr: "127.0.0.1:0", Store: store},
		log.New(io.Discard, "", 0))
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(context.Background()) }()
	for srv.Addr() == nil {
		time.Sleep(time.Millisecond)
	}

	// Open two idle connections (send nothing further) so handlers sit in ReadFrame.
	var idle []net.Conn
	for i := 0; i < 2; i++ {
		idle = append(idle, mustDial(t, srv.Addr().String()))
	}

	done := make(chan struct{})
	go func() {
		srv.Shutdown()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Shutdown did not return with idle connections open")
	}
	select {
	case err := <-serveErr:
		if err != nil {
			t.Fatalf("Serve returned error on shutdown: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Serve did not return after Shutdown")
	}
	for _, c := range idle {
		c.Close()
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	// Shutdown must be idempotent.
	srv.Shutdown()
}

func mustDial(t *testing.T, addr string) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return conn
}

func waitForValue(t *testing.T, env *testEnv, addr uint16, want uint16) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		v, err := env.store.Read(context.Background(), addr, 1)
		if err == nil && v[0] == want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("register %d never reached %04X after disconnect", addr, want)
}
