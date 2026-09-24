// Command modbus-replay is the Modbus TCP replay/test client.
//
// Subcommands:
//
//	modbus-replay write   -addr :1502 -unit 1 -at 0 -values 0x1122,0x3344 [-txn 1]
//	modbus-replay read    -addr :1502 -unit 1 -at 0 -qty 10 [-txn 2]
//	modbus-replay frag    -addr :1502 -at 0 -values ... -frag-size 1 -gap 5ms
//	modbus-replay run     -script scenario.json [-json]
//	modbus-replay verify  -db receipts.db -key-file hmac.key
//	modbus-replay list     -db receipts.db [-n 10] (receipts / exceptions)
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"modbus-receipt/internal/protocol"
	"modbus-receipt/internal/receipt"
	"modbus-receipt/internal/replay"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	sub, args := os.Args[1], os.Args[2:]
	var err error
	switch sub {
	case "write":
		err = cmdWrite(args, false)
	case "frag":
		err = cmdWrite(args, true)
	case "read":
		err = cmdRead(args)
	case "run":
		err = cmdRun(args)
	case "verify":
		err = cmdVerify(args)
	case "list":
		err = cmdList(args)
	case "-h", "--help", "help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", sub)
		usage()
		os.Exit(2)
	}
	if err != nil {
		var exc *protocol.Exception
		if errors.As(err, &exc) {
			fmt.Fprintf(os.Stderr, "MODBUS EXCEPTION: %s\n", exc)
			os.Exit(3)
		}
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `modbus-replay — Modbus TCP test/replay client

Usage:
  modbus-replay write  -addr HOST:PORT [-dial-timeout 5s] -unit U -txn T -at A -values V,V...
  modbus-replay read   -addr HOST:PORT -unit U -txn T -at A -qty N [-json]
  modbus-replay frag   -addr HOST:PORT -unit U -txn T -at A -values V,V... -frag-size B [-gap 5ms]
  modbus-replay run    -script file.json [-json]
  modbus-replay verify -db receipts.db [-key-file k]   (or MODBUS_RECEIPT_KEY)
  modbus-replay list    -db receipts.db -table receipts|exceptions [-n 10]
`)
}

type connFlags struct {
	addr     string
	timeout  time.Duration
	unit     int
	txn      int
	at       uint64
	qty      uint64
	values   string
	fragSize int
	gap      string
	asJSON   bool
}

func baseFlags(fs *flag.FlagSet) *connFlags {
	c := &connFlags{timeout: 5 * time.Second, unit: 1, txn: 1}
	fs.StringVar(&c.addr, "addr", "127.0.0.1:1502", "server host:port")
	fs.DurationVar(&c.timeout, "dial-timeout", 5*time.Second, "dial/I/O timeout")
	fs.IntVar(&c.unit, "unit", 1, "unit id")
	fs.IntVar(&c.txn, "txn", 1, "MBAP transaction id")
	fs.Uint64Var(&c.at, "at", 0, "starting register address")
	return c
}

func cmdWrite(args []string, fragmented bool) error {
	fs := flag.NewFlagSet("write", flag.ExitOnError)
	c := baseFlags(fs)
	fs.StringVar(&c.values, "values", "", "comma-separated register values (decimal or 0x hex)")
	if fragmented {
		fs.IntVar(&c.fragSize, "frag-size", 1, "bytes per TCP write")
		fs.StringVar(&c.gap, "gap", "5ms", "delay between fragments")
	}
	fs.Parse(args)

	values, err := parseValues(c.values)
	if err != nil {
		return err
	}
	conn, err := replay.Dial(c.addr, c.timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	conn.SetDeadline(c.timeout)

	var resp *protocol.ADU
	if fragmented {
		gap, err := time.ParseDuration(c.gap)
		if err != nil {
			return fmt.Errorf("bad -gap: %w", err)
		}
		resp, err = conn.WriteRegistersFragmented(uint16(c.txn), byte(c.unit), uint16(c.at), values, c.fragSize, gap)
	} else {
		resp, err = conn.WriteRegisters(uint16(c.txn), byte(c.unit), uint16(c.at), values)
	}
	if err != nil {
		return err
	}
	fmt.Printf("FC10 OK  txn=%d unit=%d echo addr=%d qty=%d (pdu=% x)\n",
		resp.TransactionID, resp.UnitID, c.at, len(values), resp.PDU)
	return nil
}

func cmdRead(args []string) error {
	fs := flag.NewFlagSet("read", flag.ExitOnError)
	c := baseFlags(fs)
	fs.Uint64Var(&c.qty, "qty", 1, "number of registers")
	fs.BoolVar(&c.asJSON, "json", false, "emit JSON")
	fs.Parse(args)

	conn, err := replay.Dial(c.addr, c.timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	conn.SetDeadline(c.timeout)

	values, resp, err := conn.ReadRegisters(uint16(c.txn), byte(c.unit), uint16(c.at), uint16(c.qty))
	if err != nil {
		return err
	}
	if c.asJSON {
		out := map[string]any{
			"transaction_id": resp.TransactionID, "unit_id": resp.UnitID,
			"address": c.at, "values": values,
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		return nil
	}
	fmt.Printf("FC03 OK  txn=%d unit=%d addr=%d qty=%d\n  values (dec): %v\n  values (hex):",
		resp.TransactionID, resp.UnitID, c.at, c.qty, values)
	for _, v := range values {
		fmt.Printf(" 0x%04x", v)
	}
	fmt.Println()
	return nil
}

func cmdRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ExitOnError)
	path := fs.String("script", "", "scenario JSON file")
	asJSON := fs.Bool("json", false, "emit machine-readable report JSON")
	fs.Parse(args)
	if *path == "" {
		return errors.New("-script is required")
	}
	sc, err := replay.LoadScript(*path)
	if err != nil {
		return err
	}
	rep := replay.Run(sc)

	if *asJSON {
		b, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(b))
	} else {
		for _, st := range rep.Steps {
			mark := "PASS"
			if !st.OK {
				mark = "FAIL"
			}
			name := st.Name
			if name == "" {
				name = st.Op
			}
			fmt.Printf("[%s] step %d %-14s %s\n", mark, st.Index, name, st.Detail)
		}
	}
	if !rep.Passed {
		return fmt.Errorf("scenario failed")
	}
	fmt.Fprintln(os.Stderr, "scenario passed")
	return nil
}

func cmdVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ExitOnError)
	dbPath := fs.String("db", "receipts.db", "SQLite database file")
	keyFile := fs.String("key-file", "", "HMAC key file")
	fs.Parse(args)
	key, err := receipt.LoadKey(*keyFile)
	if err != nil {
		return err
	}
	if len(key) == 0 {
		return errors.New("no HMAC key: -key-file or MODBUS_RECEIPT_KEY")
	}
	res, err := receipt.Verify(*dbPath, key)
	if err != nil {
		return err
	}
	if res.OK() {
		fmt.Printf("CHAIN OK: %d receipt(s), head=%s\n", res.Count, res.HeadSHA)
		return nil
	}
	fmt.Printf("CHAIN BROKEN at seq=%d: %s\n", res.FirstBroken, res.Problem)
	os.Exit(4)
	return nil
}

func cmdList(args []string) error {
	fs := flag.NewFlagSet("list", flag.ExitOnError)
	dbPath := fs.String("db", "receipts.db", "SQLite database file")
	table := fs.String("table", "receipts", "receipts or exceptions")
	n := fs.Int("n", 10, "rows to show")
	fs.Parse(args)

	key, _ := receipt.LoadKey("") // list does not need the key
	rs, err := receipt.Open(*dbPath, keyOrPlaceholder(key))
	if err != nil {
		return err
	}
	defer rs.Close()

	switch *table {
	case "receipts":
		rows, err := rs.RecentReceipts(*n)
		if err != nil {
			return err
		}
		for _, r := range rows {
			fmt.Printf("seq=%-4d txn=%-5d unit=%d addr=%-4d qty=%-3d values=%s\n        body=%s\n        chain=%s\n        hmac=%s @%s\n",
				r.Seq, r.TransactionID, r.UnitID, r.Address, r.Quantity, r.ValuesHex,
				r.BodySHA, r.ChainSHA, r.HMACSHA, r.CreatedAt)
		}
		if len(rows) == 0 {
			fmt.Println("(no receipts)")
		}
	case "exceptions":
		rows, err := rs.RecentEvents(*n)
		if err != nil {
			return err
		}
		for _, e := range rows {
			fmt.Printf("#%-4d txn=%-5d unit=%d fc=0x%02x exc=0x%02x @%s\n      %s\n",
				e.ID, e.TransactionID, e.UnitID, e.Function, e.ExceptionCode, e.CreatedAt, e.Reason)
		}
		if len(rows) == 0 {
			fmt.Println("(no exceptions)")
		}
	default:
		return fmt.Errorf("unknown table %q", *table)
	}
	return nil
}

// keyOrPlaceholder returns the configured key or a local placeholder:
// listing rows does not verify HMACs, so any non-empty key opens the DB.
func keyOrPlaceholder(key []byte) []byte {
	if len(key) > 0 {
		return key
	}
	return []byte("list-only-placeholder")
}

func parseValues(s string) ([]uint16, error) {
	if strings.TrimSpace(s) == "" {
		return nil, errors.New("-values is required")
	}
	var out []uint16
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		n, err := strconv.ParseInt(part, 0, 64)
		if err != nil {
			return nil, fmt.Errorf("bad value %q: %w", part, err)
		}
		if n < 0 || n > 0xFFFF {
			return nil, fmt.Errorf("register value %d out of 16-bit range", n)
		}
		out = append(out, uint16(n))
	}
	if len(out) == 0 || len(out) > protocol.MaxWriteQuantity {
		return nil, fmt.Errorf("quantity %d outside 1..%d", len(out), protocol.MaxWriteQuantity)
	}
	return out, nil
}
