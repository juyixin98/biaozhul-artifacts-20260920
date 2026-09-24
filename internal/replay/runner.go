package replay

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"modbus-receipt/internal/protocol"
)

// Script is a JSON replay scenario.
//
// Minimal example:
//
//	{
//	  "address": "127.0.0.1:1502",
//	  "timeout": "5s",
//	  "steps": [
//	    {"op":"write","txn":1,"unit":1,"addr":0,"values":[113,114]},
//	    {"op":"read", "txn":2,"unit":1,"addr":0,"qty":2,"expect_values":[113,114]}
//	  ]
//	}
type Script struct {
	Address string `json:"address"`
	Timeout string `json:"timeout"`
	Steps   []Step `json:"steps"`
}

// Step is one scripted action.
type Step struct {
	Name string `json:"name,omitempty"`
	// Reuse refers to a named persistent connection. A step declaring a
	// non-empty "conn" opens it on first use and keeps it open until a
	// close/abort on the same name; otherwise a fresh connection is used.
	Conn string `json:"conn,omitempty"`

	Op   string  `json:"op"`
	Txn  *uint16 `json:"txn,omitempty"`
	Unit byte    `json:"unit,omitempty"`

	Addr   uint16   `json:"addr,omitempty"`
	Qty    uint16   `json:"qty,omitempty"`
	Values []uint16 `json:"values,omitempty"`

	// Raw: arbitrary bytes to inject.
	RawHex string `json:"raw_hex,omitempty"`

	// Fragmentation / coalescing.
	FragSize int     `json:"frag_size,omitempty"`
	Gap      string  `json:"gap,omitempty"`
	Frames   []Frame `json:"frames,omitempty"` // for "coalesce": each becomes one ADU

	// Abort: send PrefixHex (possibly empty, possibly partial), then disconnect.
	PrefixHex string `json:"prefix_hex,omitempty"`
	How       string `json:"how,omitempty"` // "fin" (default) or "rst"

	Sleep string `json:"sleep,omitempty"`

	// Expectations.
	ExpectException *int     `json:"expect_exception,omitempty"` // decimal exception code, e.g. 2
	ExpectError     string   `json:"expect_error,omitempty"`     // "eof" / "closed" / "timeout"
	ExpectValues    []uint16 `json:"expect_values,omitempty"`
	ExpectTxn       *uint16  `json:"expect_txn,omitempty"`
}

// Frame is one complete ADU inside a coalesced send.
type Frame struct {
	Txn    *uint16  `json:"txn,omitempty"`
	Unit   byte     `json:"unit,omitempty"`
	RawPDU string   `json:"raw_pdu_hex,omitempty"` // raw PDU bytes (with FC)
	Op     string   `json:"op,omitempty"`          // "read"/"write" to build the PDU
	Addr   uint16   `json:"addr,omitempty"`
	Qty    uint16   `json:"qty,omitempty"`
	Values []uint16 `json:"values,omitempty"`
}

// StepResult reports the outcome of one step.
type StepResult struct {
	Index     int      `json:"index"`
	Name      string   `json:"name,omitempty"`
	Op        string   `json:"op"`
	OK        bool     `json:"ok"`
	Detail    string   `json:"detail,omitempty"`
	Exception *int     `json:"exception,omitempty"`
	Values    []uint16 `json:"values,omitempty"`
	RespTxn   *uint16  `json:"response_txn,omitempty"`
}

// Report is the full run result.
type Report struct {
	Address string       `json:"address"`
	Passed  bool         `json:"passed"`
	Steps   []StepResult `json:"steps"`
}

// LoadScript parses a scenario file.
func LoadScript(path string) (*Script, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var sc Script
	if err := json.Unmarshal(b, &sc); err != nil {
		return nil, fmt.Errorf("parsing script: %w", err)
	}
	if sc.Address == "" {
		return nil, fmt.Errorf("script missing address")
	}
	if len(sc.Steps) == 0 {
		return nil, fmt.Errorf("script has no steps")
	}
	if sc.Timeout == "" {
		sc.Timeout = "5s"
	}
	return &sc, nil
}

// Run executes the script. Expectation failures do not stop the run; the
// returned Report records each step so a CI job can see every failure.
func Run(sc *Script) Report {
	timeout, _ := time.ParseDuration(sc.Timeout)
	if timeout == 0 {
		timeout = 5 * time.Second
	}
	rep := Report{Address: sc.Address, Passed: true}
	pool := map[string]*Conn{}
	defer func() {
		for _, c := range pool {
			c.Close()
		}
	}()

	for i, st := range sc.Steps {
		res := StepResult{Index: i, Name: st.Name, Op: st.Op}
		detail, ok := runStep(sc.Address, timeout, pool, &st, &res)
		res.Detail = detail
		res.OK = ok
		if !ok {
			rep.Passed = false
		}
		rep.Steps = append(rep.Steps, res)
	}
	return rep
}

func dial(addr string, timeout time.Duration) (*Conn, error) {
	c, err := Dial(addr, timeout)
	if err == nil {
		c.SetDeadline(timeout)
	}
	return c, err
}

func runStep(addr string, timeout time.Duration, pool map[string]*Conn, st *Step, res *StepResult) (string, bool) {
	if st.Op == "sleep" {
		d, err := time.ParseDuration(st.Sleep)
		if err != nil {
			return "invalid sleep duration", false
		}
		time.Sleep(d)
		return "slept " + d.String(), true
	}

	// Resolve the connection for this step.
	var c *Conn
	var owned bool
	if st.Conn != "" {
		var ok bool
		c, ok = pool[st.Conn]
		if !ok || c == nil {
			nc, err := dial(addr, timeout)
			if err != nil {
				return checkExpectedTransport(err, st)
			}
			pool[st.Conn] = nc
			c = nc
		}
	} else {
		nc, err := dial(addr, timeout)
		if err != nil {
			return checkExpectedTransport(err, st)
		}
		c = nc
		owned = true
	}

	switch st.Op {
	case "write", "read":
		return doPDU(c, owned, st, res)
	case "frag-write":
		gap, _ := time.ParseDuration(st.Gap)
		frag := st.FragSize
		if frag <= 0 {
			frag = 1
		}
		resp, err := c.WriteRegistersFragmented(*st.Txn, orUnit(st.Unit), st.Addr, st.Values, frag, gap)
		if owned {
			c.Close()
		}
		return finishExchange(resp, err, st, res)

	case "raw":
		raw, err := hex.DecodeString(cleanHex(st.RawHex))
		if err != nil {
			return "invalid raw_hex: " + err.Error(), false
		}
		if err := c.Send(raw); err != nil {
			return checkExpectedTransport(err, st)
		}
		resp, rerr := c.ReadFrame()
		if owned {
			c.Close()
		}
		return finishExchange(resp, rerr, st, res)

	case "coalesce":
		var adus [][]byte
		for _, f := range st.Frames {
			pdu, err := framePDU(f)
			if err != nil {
				return err.Error(), false
			}
			wire, err := Marshal(orTxn(f.Txn), orUnit(f.Unit), pdu)
			if err != nil {
				return err.Error(), false
			}
			adus = append(adus, wire)
		}
		resps, err := c.SendCoalesced(adus)
		if owned {
			c.Close()
		}
		if err != nil {
			return checkExpectedTransport(err, st)
		}
		// Expectations apply per frame count; summarize exceptions.
		var codes []int
		var b strings.Builder
		for j, r := range resps {
			if r == nil {
				continue
			}
			fmt.Fprintf(&b, "[%d] txn=%d pdu=%x ", j, r.TransactionID, r.PDU)
			if r.PDU[0]&0x80 != 0 && len(r.PDU) >= 2 {
				codes = append(codes, int(r.PDU[1]))
			}
		}
		if st.ExpectException != nil {
			found := false
			for _, code := range codes {
				if code == *st.ExpectException {
					found = true
				}
			}
			if !found {
				return b.String() + "— expected exception " + fmt.Sprint(*st.ExpectException), false
			}
		}
		return "coalesced " + fmt.Sprint(len(resps)) + " responses: " + b.String(), true

	case "abort":
		prefix, err := hex.DecodeString(cleanHex(st.PrefixHex))
		if err != nil {
			return "invalid prefix_hex: " + err.Error(), false
		}
		if err := c.SendAndAbort(prefix, st.How); err != nil {
			return checkExpectedTransport(err, st)
		}
		delete(pool, st.Conn)
		return "sent " + fmt.Sprint(len(prefix)) + " bytes then disconnected (" + orHow(st.How) + ")", true

	case "close":
		how := orHow(st.How)
		var err error
		if how == "rst" {
			err = c.CloseRST()
		} else {
			err = c.Close()
		}
		if st.Conn != "" {
			delete(pool, st.Conn)
		}
		if err != nil {
			return err.Error(), false
		}
		return "connection closed (" + how + ")", true

	default:
		if owned {
			c.Close()
		}
		return "unknown op: " + st.Op, false
	}
}

func doPDU(c *Conn, owned bool, st *Step, res *StepResult) (string, bool) {
	var resp *protocol.ADU
	var values []uint16
	var err error
	if st.Op == "write" {
		resp, err = c.WriteRegisters(*st.Txn, orUnit(st.Unit), st.Addr, st.Values)
	} else {
		values, resp, err = c.ReadRegisters(*st.Txn, orUnit(st.Unit), st.Addr, st.Qty)
	}
	if owned {
		c.Close()
	}
	if err == nil && st.Op == "read" {
		res.Values = values
	}
	return finishExchange(resp, err, st, res)
}

func finishExchange(resp *protocol.ADU, err error, st *Step, res *StepResult) (string, bool) {
	if err != nil {
		// The client parsers turn an exception response into *Exception;
		// that is a valid protocol reply and must be matched against
		// expect_exception, not treated as a transport failure.
		if exc, ok := protocol.AsException(err); ok {
			code := int(exc.Code)
			res.Exception = &code
			if resp != nil {
				txn := resp.TransactionID
				res.RespTxn = &txn
				if st.ExpectTxn != nil && *st.ExpectTxn != txn {
					return fmt.Sprintf("transaction id mismatch: got %d want %d", txn, *st.ExpectTxn), false
				}
			}
			if st.ExpectException != nil && *st.ExpectException == code {
				return fmt.Sprintf("got expected exception %d (0x%02x %s)",
					code, code, protocol.ExceptionText[exc.Code]), true
			}
			return fmt.Sprintf("unexpected exception %d (0x%02x %s)",
				code, code, protocol.ExceptionText[exc.Code]), false
		}
		return checkExpectedTransport(err, st)
	}
	txn := resp.TransactionID
	res.RespTxn = &txn
	if st.ExpectTxn != nil && *st.ExpectTxn != txn {
		return fmt.Sprintf("transaction id mismatch: got %d want %d", txn, *st.ExpectTxn), false
	}

	if resp.PDU[0]&0x80 != 0 {
		code := int(resp.PDU[1])
		res.Exception = &code
		if st.ExpectException != nil && *st.ExpectException == code {
			return fmt.Sprintf("got expected exception %d (0x%02x)", code, code), true
		}
		return fmt.Sprintf("unexpected exception %d (0x%02x): %s", code, code, protocol.ExceptionText[byte(code)]), false
	}
	if st.ExpectException != nil {
		return fmt.Sprintf("expected exception %d but got normal response %x", *st.ExpectException, resp.PDU), false
	}

	if st.Op == "read" && st.ExpectValues != nil {
		if len(res.Values) < len(st.ExpectValues) {
			return fmt.Sprintf("read %d values, want %d", len(res.Values), len(st.ExpectValues)), false
		}
		for i, want := range st.ExpectValues {
			if res.Values[i] != want {
				return fmt.Sprintf("value[%d]=%d want %d (whole snapshot: %v)", i, res.Values[i], want, res.Values), false
			}
		}
	}
	return "ok", true
}

func checkExpectedTransport(err error, st *Step) (string, bool) {
	if st.ExpectError == "" {
		return "transport/frame error: " + err.Error(), false
	}
	got := classifyTransport(err)
	if got == st.ExpectError {
		return "got expected transport error: " + got, true
	}
	return fmt.Sprintf("transport error %q, wanted %q (%v)", got, st.ExpectError, err), false
}

func classifyTransport(err error) string {
	s := err.Error()
	switch {
	case strings.Contains(s, "EOF"), strings.Contains(s, "closed"), strings.Contains(s, "reset"):
		return "eof"
	case strings.Contains(s, "deadline"), strings.Contains(s, "timeout"), strings.Contains(s, "i/o timeout"):
		return "timeout"
	case strings.Contains(s, "refused"):
		return "refused"
	case strings.Contains(s, "frame error"):
		return "frame"
	default:
		return "other"
	}
}

func framePDU(f Frame) ([]byte, error) {
	if f.RawPDU != "" {
		return hex.DecodeString(cleanHex(f.RawPDU))
	}
	switch f.Op {
	case "read":
		return protocol.ReadRequestPDU(f.Addr, f.Qty), nil
	case "write":
		return protocol.WriteRequestPDU(f.Addr, f.Values), nil
	default:
		return nil, fmt.Errorf("frame must specify op read/write or raw_pdu_hex")
	}
}

func cleanHex(s string) string {
	return strings.NewReplacer(" ", "", ":", "", "\t", "", "\n", "").Replace(s)
}
func orUnit(u byte) byte {
	if u == 0 {
		return 1
	}
	return u
}
func orTxn(p *uint16) uint16 {
	if p == nil {
		return 0
	}
	return *p
}
func orHow(h string) string {
	if h == "" {
		return "fin"
	}
	return h
}
