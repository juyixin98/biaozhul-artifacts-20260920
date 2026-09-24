package protocol

import (
	"bytes"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"os"
	"testing"
)

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

// A textbook FC03 request: txn 0x0001, proto 0, length 6, unit 1,
// FC03 start 0 qty 10.
const fc03ReqHex = "00010000000601030000000a"

func TestReadFrame_FC03(t *testing.T) {
	f, err := ReadFrame(bytes.NewReader(mustHex(t, fc03ReqHex)))
	if err != nil {
		t.Fatal(err)
	}
	if f.TransactionID != 1 || f.ProtocolID != 0 || f.UnitID != 1 {
		t.Fatalf("mbap mismatch: %+v", f)
	}
	r, exc, ok := ParseReadHoldingRegistersRequest(f.PDU)
	if !ok {
		t.Fatalf("parse failed exc=0x%02X", exc)
	}
	if r.StartAddress != 0 || r.Quantity != 10 {
		t.Fatalf("pdu mismatch: %+v", r)
	}
}

// FC10: txn 4, unit 1, write 3 registers at 10: 1,2,3.
const fc10ReqHex = "00040000000d0110000a000306000100020003"

func TestReadFrame_FC10(t *testing.T) {
	f, err := ReadFrame(bytes.NewReader(mustHex(t, fc10ReqHex)))
	if err != nil {
		t.Fatal(err)
	}
	w, exc, ok := ParseWriteMultipleRegistersRequest(f.PDU)
	if !ok {
		t.Fatalf("parse failed exc=0x%02X", exc)
	}
	if w.StartAddress != 10 || len(w.Values) != 3 ||
		w.Values[0] != 1 || w.Values[1] != 2 || w.Values[2] != 3 {
		t.Fatalf("pdu mismatch: %+v", w)
	}
}

// TCP segmentation: feed the same FC03 frame one byte at a time over a real
// TCP socket; ReadFrame must transparently reassemble it ("分包").
func TestReadFrame_ByteByByteOverTCP(t *testing.T) {
	raw := mustHex(t, fc03ReqHex)
	srv, cli := net.Pipe()
	go func() {
		for _, b := range raw {
			_, _ = cli.Write([]byte{b})
		}
	}()
	f, err := ReadFrame(srv)
	if err != nil {
		t.Fatal(err)
	}
	if f.TransactionID != 1 || f.FunctionCode() != FuncReadHoldingRegisters {
		t.Fatalf("bad frame: %+v", f)
	}
}

// Coalesced back-to-back frames ("粘包"): two complete ADUs in one buffer must
// parse as exactly two frames, never merged.
func TestReadFrame_CoalescedFrames(t *testing.T) {
	two := append(mustHex(t, fc03ReqHex), mustHex(t, fc10ReqHex)...)
	r := bytes.NewReader(two)
	f1, err := ReadFrame(r)
	if err != nil {
		t.Fatal(err)
	}
	f2, err := ReadFrame(r)
	if err != nil {
		t.Fatal(err)
	}
	if f1.TransactionID != 1 || f1.FunctionCode() != 0x03 {
		t.Fatalf("frame1: %+v", f1)
	}
	if f2.TransactionID != 4 || f2.FunctionCode() != 0x10 {
		t.Fatalf("frame2: %+v", f2)
	}
}

// Mixed: two frames sent as [header1...1byte][rest1+all of frame2].
func TestReadFrame_ArbitrarySegmentBoundary(t *testing.T) {
	two := append(mustHex(t, fc03ReqHex), mustHex(t, fc10ReqHex)...)
	srv, cli := net.Pipe()
	cut := 9
	go func() {
		_, _ = cli.Write(two[:cut])
		_, _ = cli.Write(two[cut:])
	}()
	for i, wantTxn := range []uint16{1, 4} {
		f, err := ReadFrame(srv)
		if err != nil {
			t.Fatalf("frame %d: %v", i, err)
		}
		if f.TransactionID != wantTxn {
			t.Fatalf("frame %d txn=%d want %d", i, f.TransactionID, wantTxn)
		}
	}
}

// A stream that ends mid-frame must report ErrTruncated, not a half frame.
func TestReadFrame_Truncated(t *testing.T) {
	half := mustHex(t, fc10ReqHex)[:9] // header + 2 pdu bytes
	_, err := ReadFrame(bytes.NewReader(half))
	if !errors.Is(err, ErrTruncated) {
		t.Fatalf("want ErrTruncated, got %v", err)
	}
}

// Truncated exactly at the header boundary is ErrTruncated too.
func TestReadFrame_TruncatedHeader(t *testing.T) {
	_, err := ReadFrame(bytes.NewReader([]byte{0, 1, 0, 0}))
	if err != io.EOF && !errors.Is(err, ErrTruncated) {
		t.Fatalf("want eof/truncated, got %v", err)
	}
}

func TestReadFrame_LengthTooLarge(t *testing.T) {
	// length = 0x0100 (256) > 254
	bad := mustHex(t, "000100000100010300")
	_, err := ReadFrame(bytes.NewReader(bad))
	if !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("want ErrFrameTooLarge, got %v", err)
	}
}

func TestReadFrame_LengthZero(t *testing.T) {
	bad := mustHex(t, "000100000000")
	if _, err := ReadFrame(bytes.NewReader(bad)); err == nil {
		t.Fatal("want error for zero length")
	}
}

func TestFC03_QuantityLimits(t *testing.T) {
	for _, q := range []uint16{0, 126, 65535} {
		pdu := ReadHoldingRegistersRequest{StartAddress: 0, Quantity: q}.Encode()
		if _, exc, ok := ParseReadHoldingRegistersRequest(pdu); ok {
			t.Fatalf("qty %d should be rejected", q)
		} else if exc != ExcIllegalDataValue {
			t.Fatalf("qty %d exc=0x%02X want 0x03", q, exc)
		}
	}
}

func TestFC10_QuantityLimits(t *testing.T) {
	// qty 0
	pdu := []byte{FuncWriteMultipleRegisters, 0, 0, 0, 0, 0}
	if _, exc, ok := ParseWriteMultipleRegistersRequest(pdu); ok || exc != ExcIllegalDataValue {
		t.Fatalf("qty 0: ok=%v exc=0x%02X", ok, exc)
	}
	// qty 124 > 123
	pdu = make([]byte, 13+2*124)
	pdu[0] = FuncWriteMultipleRegisters
	pdu[3], pdu[4] = 0, 124
	pdu[5] = byte(2 * 124)
	if _, exc, ok := ParseWriteMultipleRegistersRequest(pdu); ok || exc != ExcIllegalDataValue {
		t.Fatalf("qty 124: ok=%v exc=0x%02X", ok, exc)
	}
	// byte count mismatch
	pdu = WriteMultipleRegistersRequest{StartAddress: 0, Values: []uint16{1, 2, 3}}.Encode()
	pdu[5] = 4 // claims 2 registers, declares 3
	if _, exc, ok := ParseWriteMultipleRegistersRequest(pdu); ok || exc != ExcIllegalDataValue {
		t.Fatalf("bad bytecount: ok=%v exc=0x%02X", ok, exc)
	}
}

func TestResponseRoundTrips(t *testing.T) {
	values := []uint16{0x1234, 0xABCD, 0x0000, 0xFFFF}
	pdu := EncodeReadHoldingRegistersResponse(values)
	got, err := DecodeReadHoldingRegistersResponse(pdu)
	if err != nil {
		t.Fatal(err)
	}
	for i := range values {
		if got[i] != values[i] {
			t.Fatalf("index %d: got %04X want %04X", i, got[i], values[i])
		}
	}
}

func TestExceptionEncoding(t *testing.T) {
	pdu := ExceptionResponse(FuncWriteMultipleRegisters, ExcIllegalDataAddress)
	if pdu[0] != FuncWriteMultipleRegisters|0x80 || pdu[1] != ExcIllegalDataAddress {
		t.Fatalf("bad exception pdu: % X", pdu)
	}
	code, isExc := AsException(pdu)
	if !isExc || code != ExcIllegalDataAddress {
		t.Fatal("AsException failed")
	}
	if _, isExc := AsException([]byte{FuncReadHoldingRegisters, 2}); isExc {
		t.Fatal("normal response misread as exception")
	}
}

func TestFrameEncodeLayout(t *testing.T) {
	f := Frame{TransactionID: 0x1122, ProtocolID: 0, UnitID: 0x09,
		PDU: []byte{FuncReadHoldingRegisters, 0, 0, 0, 1}}
	got := hex.EncodeToString(f.Encode())
	want := "112200000006090300000001"
	if got != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestMain(m *testing.M) { os.Exit(m.Run()) }
