// Package replay parses and executes JSONL scenario files. Each non-empty
// line is one JSON object; lines beginning with '#' are comments.
//
// Operation types:
//
//	{"op":"read","txn":7,"unit":1,"start":0,"qty":4}
//	{"op":"write","txn":8,"unit":1,"start":10,"values":[1,2,3]}
//	{"op":"raw","bytes":"hex string", "expect_response":true}
//	    — send arbitrary bytes (used to split one frame across TCP segments
//	    and to send multiple frames in one write: 粘包/分包)
//	{"op":"recv","txn":7}                     — read one ADU, optionally check txn
//	{"op":"disconnect"}                       — close the TCP connection now
//	{"op":"connect"}                          — (re)open a connection
//	{"op":"sleep","ms":200}
//
// Any step may carry "expect_error":"0x03"; the step passes only if the
// server response or transport error contains that substring.
//
// "txn" may be omitted or 0 for auto-assigned IDs; explicit repeats are
// allowed (and NOT deduplicated — see README).
package replay

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"modbus-receipt/internal/client"
	"modbus-receipt/internal/protocol"
)

// Step is one decoded scenario line.
type Step struct {
	LineNo int `json:"-"`

	Op          string   `json:"op"`
	Txn         uint16   `json:"txn"`
	Unit        byte     `json:"unit"`
	Start       uint16   `json:"start"`
	Qty         uint16   `json:"qty"`
	Values      []uint16 `json:"values"`
	Bytes       string   `json:"bytes"` // hex
	Expect      *bool    `json:"expect_response"`
	ExpectError string   `json:"expect_error"`
	Ms          int      `json:"ms"`
}

// Result is the outcome of executing one step.
type Result struct {
	LineNo    int      `json:"line"`
	Op        string   `json:"op"`
	OK        bool     `json:"ok"`
	Txn       uint16   `json:"txn,omitempty"`
	Values    []uint16 `json:"values,omitempty"`
	EchoStart uint16   `json:"echo_start,omitempty"`
	EchoQty   uint16   `json:"echo_qty,omitempty"`
	Error     string   `json:"error,omitempty"`
	RawResp   string   `json:"raw_response_hex,omitempty"`
}

// ParseFile reads a JSONL scenario file.
func ParseFile(path string) ([]Step, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return Parse(f)
}

// Parse decodes a JSONL scenario stream.
func Parse(r io.Reader) ([]Step, error) {
	var steps []Step
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 1024*1024), 4*1024*1024)
	line := 0
	for sc.Scan() {
		line++
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		var st Step
		if err := json.Unmarshal([]byte(text), &st); err != nil {
			return nil, fmt.Errorf("line %d: %w", line, err)
		}
		st.LineNo = line
		if err := validate(st); err != nil {
			return nil, fmt.Errorf("line %d: %w", line, err)
		}
		steps = append(steps, st)
	}
	return steps, sc.Err()
}

func validate(st Step) error {
	switch st.Op {
	case "read":
		if st.Qty < 1 || st.Qty > protocol.MaxReadQuantity {
			return fmt.Errorf("read qty must be 1..%d", protocol.MaxReadQuantity)
		}
	case "write":
		if len(st.Values) < 1 || len(st.Values) > protocol.MaxWriteQuantity {
			return fmt.Errorf("write values must be 1..%d registers", protocol.MaxWriteQuantity)
		}
	case "raw":
		if st.Bytes == "" {
			return fmt.Errorf("raw step needs non-empty hex \"bytes\"")
		}
		if _, err := hex.DecodeString(st.Bytes); err != nil {
			return fmt.Errorf("raw bytes not valid hex: %w", err)
		}
	case "sleep":
		if st.Ms < 0 {
			return fmt.Errorf("sleep ms must be >= 0")
		}
	case "recv", "disconnect", "connect":
	default:
		return fmt.Errorf("unknown op %q", st.Op)
	}
	return nil
}

// Runner executes scenarios against one server address, creating fresh
// client connections as directed.
type Runner struct {
	Addr        string
	DialTimeout time.Duration

	conn *client.Client
}

// NewRunner creates a runner. It does not connect until the first step that
// needs one (an implicit connect happens before read/write/raw).
func NewRunner(addr string, dialTimeout time.Duration) *Runner {
	return &Runner{Addr: addr, DialTimeout: dialTimeout}
}

// Close releases any open connection.
func (r *Runner) Close() error {
	if r.conn != nil {
		return r.conn.Close()
	}
	return nil
}

func (r *Runner) ensureConn() (*client.Client, error) {
	if r.conn != nil {
		return r.conn, nil
	}
	c, err := client.Dial(r.Addr, r.DialTimeout)
	if err != nil {
		return nil, err
	}
	r.conn = c
	return c, nil
}

// Run executes all steps, returning one Result per step. Execution continues
// after step errors (so a scenario can observe exception responses); it only
// stops early on "disconnect" handling or a raw step that tears the stream.
func (r *Runner) Run(steps []Step) []Result {
	out := make([]Result, 0, len(steps))
	for _, st := range steps {
		out = append(out, r.runOne(st))
	}
	return out
}

func (r *Runner) runOne(st Step) Result {
	res := Result{LineNo: st.LineNo, Op: st.Op, Txn: st.Txn}
	switch st.Op {
	case "connect":
		if r.conn != nil {
			_ = r.conn.Close()
		}
		c, err := client.Dial(r.Addr, r.DialTimeout)
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		r.conn = c
		res.OK = true
	case "disconnect":
		if r.conn != nil {
			_ = r.conn.Close()
			r.conn = nil
		}
		res.OK = true
	case "sleep":
		time.Sleep(time.Duration(st.Ms) * time.Millisecond)
		res.OK = true
	case "read":
		c, err := r.ensureConn()
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		values, _, err := c.ReadHoldingRegisters(st.Txn, st.Unit, st.Start, st.Qty)
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		res.OK, res.Values = true, values
	case "write":
		c, err := r.ensureConn()
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		echoStart, echoQty, _, err := c.WriteMultipleRegisters(st.Txn, st.Unit, st.Start, st.Values)
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		res.OK, res.EchoStart, res.EchoQty = true, echoStart, echoQty
	case "recv":
		c, err := r.ensureConn()
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		f, err := c.ReadOneFrame()
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		res.RawResp = hex.EncodeToString(f.Encode())
		if code, isExc := protocol.AsException(f.PDU); isExc {
			res.Error = fmt.Sprintf("exception 0x%02X", code)
			r.evaluateExpect(&res, st)
			return res
		}
		if st.Txn != 0 && f.TransactionID != st.Txn {
			r.fail(&res, st, fmt.Errorf("%w: got %d", client.ErrTransactionMismatch, f.TransactionID))
			return res
		}
		res.OK = true
	case "raw":
		c, err := r.ensureConn()
		if err != nil {
			r.fail(&res, st, err)
			return res
		}
		raw, _ := hex.DecodeString(st.Bytes)
		if err := c.SendRawBytes(raw); err != nil {
			r.fail(&res, st, err)
			return res
		}
		if st.Expect != nil && *st.Expect {
			f, err := c.ReadOneFrame()
			if err != nil {
				r.fail(&res, st, err)
				return res
			}
			res.RawResp = hex.EncodeToString(f.Encode())
			if code, isExc := protocol.AsException(f.PDU); isExc {
				res.Error = fmt.Sprintf("exception 0x%02X", code)
				r.evaluateExpect(&res, st)
				return res
			}
		}
		res.OK = true
	}
	r.evaluateExpect(&res, st)
	return res
}

// fail records an error on the result, then applies an optional expect_error
// assertion (so a scenario can REQUIRE a specific failure and pass).
func (r *Runner) fail(res *Result, st Step, err error) {
	res.Error = err.Error()
	r.evaluateExpect(res, st)
}

// evaluateExpect marks the step OK when the observed error satisfies
// expect_error (substring match); an expect_error with no matching error is a
// failure, and an unexpected error remains a failure.
func (r *Runner) evaluateExpect(res *Result, st Step) {
	want := st.ExpectError
	if want == "" {
		return
	}
	if res.Error != "" && strings.Contains(res.Error, want) {
		res.OK = true
		return
	}
	res.OK = false
	if res.Error == "" {
		res.Error = "expected error containing " + want + " but step succeeded"
	} else {
		res.Error = "expected error containing " + want + ", got: " + res.Error
	}
}
