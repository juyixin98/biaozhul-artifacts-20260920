// Command modbusctl is the Modbus/TCP replay client and receipt inspector.
//
// Subcommands:
//
//	modbusctl read   -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS QUANTITY
//	modbusctl write  -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS V1 V2 ...
//	modbusctl replay -a HOST:PORT scenario.jsonl [-json]
//	modbusctl receipts -db data/modbus.db [--limit N] [--json]
//	modbusctl verify   -db data/modbus.db
//
// Values may be decimal (1234), 0x-prefixed hex (0x04D2) or binary (0b101).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"modbus-receipt/internal/client"
	"modbus-receipt/internal/replay"
	"modbus-receipt/internal/storage"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "read":
		err = cmdRead(os.Args[2:])
	case "write":
		err = cmdWrite(os.Args[2:])
	case "replay":
		err = cmdReplay(os.Args[2:])
	case "receipts":
		err = cmdReceipts(os.Args[2:])
	case "verify":
		err = cmdVerify(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `modbusctl — Modbus/TCP replay client & receipt inspector

Usage:
  modbusctl read     -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS QUANTITY
  modbusctl write    -a HOST:PORT [-u UNIT] [-t TXN] ADDRESS V1 [V2 ...]
  modbusctl replay   -a HOST:PORT [-json] SCENARIO.jsonl
  modbusctl receipts -db PATH [-limit N] [-json]
  modbusctl verify   -db PATH

-t 0 (default) auto-assigns Transaction IDs; any explicit value is sent as-is
(duplicates are legal in Modbus and are NOT deduplicated).
`)
}

type netFlags struct {
	addr string
	unit uint
	txn  uint
	to   time.Duration
}

func addNetFlags(fs *flag.FlagSet) *netFlags {
	nf := &netFlags{}
	fs.StringVar(&nf.addr, "a", "127.0.0.1:1502", "server address host:port")
	fs.UintVar(&nf.unit, "u", 1, "unit id (0..255)")
	fs.UintVar(&nf.txn, "t", 0, "transaction id (0 = auto-assign)")
	fs.DurationVar(&nf.to, "timeout", 5*time.Second, "dial timeout")
	return nf
}

func cmdRead(args []string) error {
	fs := flag.NewFlagSet("read", flag.ContinueOnError)
	nf := addNetFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 2 {
		return fmt.Errorf("read needs ADDRESS and QUANTITY")
	}
	start, err := parseUint16(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("address: %w", err)
	}
	qty, err := parseUint16(fs.Arg(1))
	if err != nil {
		return fmt.Errorf("quantity: %w", err)
	}
	c, err := client.Dial(nf.addr, nf.to)
	if err != nil {
		return err
	}
	defer c.Close()
	values, resp, err := c.ReadHoldingRegisters(uint16(nf.txn), byte(nf.unit), start, qty)
	if err != nil {
		return err
	}
	fmt.Printf("FC03 response txn=%d unit=%d start=%d qty=%d\n",
		resp.TransactionID, resp.UnitID, start, len(values))
	for i, v := range values {
		fmt.Printf("  [%5d] %5d  0x%04X\n", int(start)+i, v, v)
	}
	return nil
}

func cmdWrite(args []string) error {
	fs := flag.NewFlagSet("write", flag.ContinueOnError)
	nf := addNetFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() < 2 {
		return fmt.Errorf("write needs ADDRESS and at least one VALUE")
	}
	start, err := parseUint16(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("address: %w", err)
	}
	values := make([]uint16, 0, fs.NArg()-1)
	for _, a := range fs.Args()[1:] {
		v, err := parseUint16(a)
		if err != nil {
			return fmt.Errorf("value %q: %w", a, err)
		}
		values = append(values, v)
	}
	c, err := client.Dial(nf.addr, nf.to)
	if err != nil {
		return err
	}
	defer c.Close()
	echoStart, echoQty, resp, err := c.WriteMultipleRegisters(
		uint16(nf.txn), byte(nf.unit), start, values)
	if err != nil {
		return err
	}
	fmt.Printf("FC10 response txn=%d unit=%d echo: start=%d qty=%d\n",
		resp.TransactionID, resp.UnitID, echoStart, echoQty)
	return nil
}

func cmdReplay(args []string) error {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	addr := fs.String("a", "127.0.0.1:1502", "server address host:port")
	asJSON := fs.Bool("json", false, "emit machine-readable JSON results")
	to := fs.Duration("timeout", 5*time.Second, "dial timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("replay needs one SCENARIO.jsonl file")
	}
	steps, err := replay.ParseFile(fs.Arg(0))
	if err != nil {
		return err
	}
	r := replay.NewRunner(*addr, *to)
	defer r.Close()
	results := r.Run(steps)

	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(results)
	}
	failures := 0
	for _, res := range results {
		mark := "ok  "
		if !res.OK {
			mark = "FAIL"
			failures++
		}
		line := fmt.Sprintf("%s line=%d op=%s", mark, res.LineNo, res.Op)
		if res.Txn != 0 {
			line += fmt.Sprintf(" txn=%d", res.Txn)
		}
		switch {
		case res.Values != nil:
			line += fmt.Sprintf(" values=%s", fmtU16(res.Values))
		case res.EchoQty != 0:
			line += fmt.Sprintf(" echo(start=%d qty=%d)", res.EchoStart, res.EchoQty)
		}
		if res.Error != "" {
			line += " error=" + res.Error
		}
		if res.RawResp != "" {
			line += " raw=" + res.RawResp
		}
		fmt.Println(line)
	}
	fmt.Printf("\n%d step(s), %d failure(s)\n", len(results), failures)
	if failures > 0 {
		return fmt.Errorf("scenario reported %d failure(s)", failures)
	}
	return nil
}

func cmdReceipts(args []string) error {
	fs := flag.NewFlagSet("receipts", flag.ContinueOnError)
	db := fs.String("db", "data/modbus.db", "SQLite database path")
	limit := fs.Int("limit", 20, "max receipts to show (0 = all)")
	asJSON := fs.Bool("json", false, "emit JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN: "file:" + *db + "?mode=ro", BankSize: 1, AllowedUnits: []byte{1}, ReadOnly: true,
	})
	if err != nil {
		return err
	}
	defer store.Close()
	receipts, err := store.ListReceipts(context.Background(), *limit)
	if err != nil {
		return err
	}
	if *asJSON {
		type jReceipt struct {
			Seq           int64    `json:"seq"`
			TransactionID uint16   `json:"transaction_id"`
			UnitID        byte     `json:"unit_id"`
			StartAddress  uint16   `json:"start_address"`
			Quantity      uint16   `json:"quantity"`
			Values        []uint16 `json:"values"`
			ClientAddr    string   `json:"client_addr"`
			CommittedAt   string   `json:"committed_at"`
			PrevHash      string   `json:"prev_hash"`
			Hash          string   `json:"hash"`
		}
		out := make([]jReceipt, len(receipts))
		for i, r := range receipts {
			out[i] = jReceipt{r.Seq, r.TransactionID, r.UnitID, r.StartAddress,
				r.Quantity, r.Values, r.ClientAddr,
				r.CommittedAt.Format("2006-01-02T15:04:05.000000000Z07:00"),
				r.PrevHash, r.Hash}
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(out)
	}
	for _, r := range receipts {
		fmt.Printf("#%-4d txn=%-5d unit=%d addr=%-4d qty=%-3d %s\n",
			r.Seq, r.TransactionID, r.UnitID, r.StartAddress, r.Quantity,
			r.CommittedAt.Format(time.RFC3339Nano))
		fmt.Printf("       client=%s values=%s\n", r.ClientAddr, fmtU16(r.Values))
		fmt.Printf("       prev=%s\n", r.PrevHash)
		fmt.Printf("       hash=%s\n", r.Hash)
	}
	fmt.Printf("\n%d receipt(s)\n", len(receipts))
	return nil
}

func cmdVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	db := fs.String("db", "data/modbus.db", "SQLite database path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	store, err := storage.NewStore(context.Background(), storage.Options{
		DSN: "file:" + *db + "?mode=ro", BankSize: 1, AllowedUnits: []byte{1}, ReadOnly: true,
	})
	if err != nil {
		return err
	}
	defer store.Close()
	broken, err := store.VerifyChain(context.Background())
	if err != nil {
		return err
	}
	if broken < 0 {
		rs, _ := store.ListReceipts(context.Background(), 0)
		fmt.Printf("OK: receipt hash chain intact (%d receipts)\n", len(rs))
		return nil
	}
	fmt.Printf("TAMPERED: hash chain broken at receipt seq %d\n", broken)
	os.Exit(3)
	return nil
}

func parseUint16(s string) (uint16, error) {
	s = strings.TrimSpace(s)
	var (
		n   uint64
		err error
	)
	switch {
	case strings.HasPrefix(strings.ToLower(s), "0x"):
		n, err = strconv.ParseUint(s[2:], 16, 16)
	case strings.HasPrefix(s, "0b"):
		n, err = strconv.ParseUint(s[2:], 2, 16)
	default:
		n, err = strconv.ParseUint(s, 10, 16)
	}
	if err != nil {
		return 0, err
	}
	return uint16(n), nil
}

func fmtU16(vs []uint16) string {
	parts := make([]string, len(vs))
	for i, v := range vs {
		parts[i] = strconv.FormatUint(uint64(v), 10)
	}
	return "[" + strings.Join(parts, ",") + "]"
}
