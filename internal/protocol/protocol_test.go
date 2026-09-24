package protocol

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"
)

func TestMarshalRoundTrip(t *testing.T) {
	adu := &ADU{
		TransactionID: 0x1234,
		ProtocolID:    0,
		UnitID:        7,
		PDU:           WriteRequestPDU(10, []uint16{0xAABB, 0xCCDD}),
	}
	wire, err := adu.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if len(wire) != MBAPHeaderSize+len(adu.PDU) {
		t.Fatalf("len=%d", len(wire))
	}
	if got := binary.BigEndian.Uint16(wire[4:6]); int(got) != len(adu.PDU)+1 {
		t.Fatalf("length field=%d want %d", got, len(adu.PDU)+1)
	}
	got, err := ReadADU(bytes.NewReader(wire))
	if err != nil {
		t.Fatal(err)
	}
	if got.TransactionID != 0x1234 || got.UnitID != 7 {
		t.Fatalf("header mismatch: %+v", got)
	}
	if !bytes.Equal(got.PDU, adu.PDU) {
		t.Fatalf("pdu mismatch: %x vs %x", got.PDU, adu.PDU)
	}
}

// slowReader returns the buffered data one byte at a time.
type slowReader struct{ data []byte }

func (s *slowReader) Read(p []byte) (int, error) {
	if len(s.data) == 0 {
		return 0, io.EOF
	}
	p[0] = s.data[0]
	s.data = s.data[1:]
	return 1, nil
}

func TestReadADU_ByteAtATime(t *testing.T) {
	adu := &ADU{TransactionID: 42, UnitID: 1, PDU: WriteRequestPDU(0, []uint16{1, 2, 3})}
	wire, _ := adu.Marshal()
	got, err := ReadADU(&slowReader{data: wire})
	if err != nil {
		t.Fatalf("fragmented read failed: %v", err)
	}
	if got.TransactionID != 42 || len(got.PDU) != len(adu.PDU) {
		t.Fatalf("mismatch: %+v", got)
	}
}

func TestReadADU_CoalescedTwoADUs(t *testing.T) {
	a1, _ := (&ADU{TransactionID: 1, UnitID: 1, PDU: ReadRequestPDU(0, 10)}).Marshal()
	a2, _ := (&ADU{TransactionID: 2, UnitID: 1, PDU: ReadRequestPDU(5, 2)}).Marshal()
	r := bytes.NewReader(append(a1, a2...))
	first, err := ReadADU(r)
	if err != nil || first.TransactionID != 1 {
		t.Fatalf("first: %+v err=%v", first, err)
	}
	second, err := ReadADU(r)
	if err != nil || second.TransactionID != 2 {
		t.Fatalf("second: %+v err=%v", second, err)
	}
}

func TestReadADU_PartialThenEOF(t *testing.T) {
	wire, _ := (&ADU{TransactionID: 1, UnitID: 1, PDU: ReadRequestPDU(0, 1)}).Marshal()
	for _, cut := range []int{1, 3, 6, 7, 9} {
		if _, err := ReadADU(bytes.NewReader(wire[:cut])); !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("cut=%d: want ErrUnexpectedEOF, got %v", cut, err)
		}
	}
}

func TestReadADU_CleanEOF(t *testing.T) {
	if _, err := ReadADU(bytes.NewReader(nil)); !errors.Is(err, io.EOF) {
		t.Fatalf("want io.EOF, got %v", err)
	}
}

func TestReadADU_BadLength(t *testing.T) {
	bad := []byte{0, 1, 0, 0, 0, 1, 1} // length=1 but minimum is 3
	_, err := ReadADU(bytes.NewReader(bad))
	var fe *FrameError
	if !errors.As(err, &fe) {
		t.Fatalf("want FrameError, got %T %v", err, err)
	}
}

func TestReadADU_WrongProtocolID(t *testing.T) {
	adu := &ADU{TransactionID: 1, ProtocolID: 1, UnitID: 1, PDU: ReadRequestPDU(0, 1)}
	wire, _ := adu.Marshal()
	_, err := ReadADU(bytes.NewReader(wire))
	var fe *FrameError
	if !errors.As(err, &fe) {
		t.Fatalf("want FrameError, got %T %v", err, err)
	}
}

// lengthLyingReader declares a PDU longer than the bytes actually present.
type lengthLyingReader struct{}

func (lengthLyingReader) Read(p []byte) (int, error) { return 0, io.EOF }

func TestReadADU_DeclaredLengthBeyondStream(t *testing.T) {
	wire, _ := (&ADU{TransactionID: 1, UnitID: 1, PDU: ReadRequestPDU(0, 1)}).Marshal()
	binary.BigEndian.PutUint16(wire[4:6], 200) // claim 199 PDU bytes, only 4 present
	if _, err := ReadADU(bytes.NewReader(wire)); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("want ErrUnexpectedEOF, got %v", err)
	}
}

func TestDecodeRequest_Read(t *testing.T) {
	req, err := DecodeRequest(ReadRequestPDU(20, 125))
	if err != nil || req.Address != 20 || req.Quantity != 125 {
		t.Fatalf("%+v err=%v", req, err)
	}
}

func TestDecodeRequest_ReadBadQuantity(t *testing.T) {
	for _, q := range []uint16{0, 126, 1000} {
		_, err := DecodeRequest(ReadRequestPDU(0, q))
		var re *RequestError
		if !errors.As(err, &re) || re.Code != ExcIllegalDataValue {
			t.Fatalf("qty=%d want ILLEGAL DATA VALUE, got %v", q, err)
		}
	}
}

func TestDecodeRequest_Write(t *testing.T) {
	req, err := DecodeRequest(WriteRequestPDU(3, []uint16{0x0102, 0x0304}))
	if err != nil {
		t.Fatal(err)
	}
	if req.Address != 3 || req.Quantity != 2 || req.Values[1] != 0x0304 {
		t.Fatalf("%+v", req)
	}
}

func TestDecodeRequest_WriteByteCountMismatch(t *testing.T) {
	pdu := WriteRequestPDU(0, []uint16{1, 2})
	pdu[5] = 3 // lie about byte count
	_, err := DecodeRequest(pdu)
	var re *RequestError
	if !errors.As(err, &re) || re.Code != ExcIllegalDataValue {
		t.Fatalf("want ILLEGAL DATA VALUE, got %v", err)
	}
}

func TestDecodeRequest_UnknownFunction(t *testing.T) {
	_, err := DecodeRequest([]byte{0x04, 0, 0, 0, 1}) // FC04 (read input) unsupported
	var re *RequestError
	if !errors.As(err, &re) || re.Code != ExcIllegalFunction {
		t.Fatalf("want ILLEGAL FUNCTION, got %v", err)
	}
}

func TestResponseParsers_Exception(t *testing.T) {
	if _, err := ParseReadResponse(ExceptionPDU(FCReadHoldingRegisters, ExcIllegalDataAddress)); err == nil {
		t.Fatal("expected exception error")
	} else {
		exc, ok := AsException(err)
		if !ok || exc.Code != ExcIllegalDataAddress || exc.Function != FCReadHoldingRegisters {
			t.Fatalf("bad exception: %+v", exc)
		}
	}
}

func TestReadResponseRoundTrip(t *testing.T) {
	want := []uint16{0xDEAD, 0xBEEF, 0}
	got, err := ParseReadResponse(ReadResponsePDU(want))
	if err != nil {
		t.Fatal(err)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("index %d: %x vs %x", i, got[i], want[i])
		}
	}
}
