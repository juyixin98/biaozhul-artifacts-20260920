// Command fixturetool works with the deterministic local signing fixtures:
// it prints the public keys and generates fully-signed example HTTP request
// bodies for the three acceptance scenarios (basic, reorg, conflict).
package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"inbox/internal/fixtures"
	"inbox/internal/wire"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "keys":
		cmdKeys()
	case "generate":
		dir := "examples"
		if len(os.Args) >= 3 {
			dir = os.Args[2]
		}
		if err := cmdGenerate(dir); err != nil {
			fail(err)
		}
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: fixturetool keys | generate [output-dir]")
	os.Exit(2)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}

func hx(b []byte) string { return hex.EncodeToString(b) }

func cmdKeys() {
	for _, id := range fixtures.ChainIDs() {
		cf := fixtures.Chains[id]
		fmt.Printf("%s validator %s\n", cf.ID, hx(cf.Validator.Pub))
		for _, s := range cf.Senders {
			fmt.Printf("%s sender    %s %s\n", cf.ID, s.Name, hx(s.Pub))
		}
	}
}

// scenario is a generated set of request files plus an ordered playbook.
type scenario struct {
	dir      string
	open     []wire.OpenChannelRequest
	headers  []wire.HeaderRequest
	revokes  []wire.RevocationRequest
	messages []namedMessage
	steps    []string
}

type namedMessage struct {
	name string
	req  wire.MessageRequest
}

func cmdGenerate(root string) error {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return err
	}
	for _, build := range []struct {
		name string
		fn   func() (*scenario, error)
	}{
		{"basic", genBasic},
		{"reorg", genReorg},
		{"conflict", genConflict},
	} {
		sc, err := build.fn()
		if err != nil {
			return err
		}
		sc.dir = filepath.Join(root, build.name)
		if err := writeScenario(sc); err != nil {
			return err
		}
		fmt.Printf("generated %s (%d steps)\n", sc.dir, len(sc.steps))
	}
	return nil
}

func headerReq(b fixtures.BuiltBlock) wire.HeaderRequest {
	return wire.HeaderRequest{
		ChainID: b.ChainID, Height: b.Height,
		ParentHex:       hx(b.Parent[:]),
		MsgRootHex:      hx(b.MsgRoot[:]),
		Timestamp:       b.Timestamp,
		ValidatorPubHex: hx(b.Validator.Pub),
		SignatureHex:    hx(b.Sig),
	}
}

func messageReq(m fixtures.BuiltMessage) wire.MessageRequest {
	steps := make([]wire.ProofStepDTO, len(m.Proof.Steps))
	for i, st := range m.Proof.Steps {
		steps[i] = wire.ProofStepDTO{SiblingHex: hx(st.Sibling[:]), Side: int(st.Side)}
	}
	return wire.MessageRequest{
		ChainID: m.ChainID, ChannelID: m.ChannelID, Nonce: m.Nonce,
		PayloadHex:   hx(m.Payload),
		SenderPubHex: hx(m.Sender.Pub),
		SenderSigHex: hx(m.SenderSig),
		BlockHex:     hx(m.Block[:]),
		Proof:        steps,
	}
}

func writeJSON(path string, v any) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func writeScenario(sc *scenario) error {
	if err := os.MkdirAll(sc.dir, 0o755); err != nil {
		return err
	}
	for i, r := range sc.open {
		if err := writeJSON(filepath.Join(sc.dir, fmt.Sprintf("open-%d.json", i+1)), r); err != nil {
			return err
		}
	}
	for i, r := range sc.headers {
		if err := writeJSON(filepath.Join(sc.dir, fmt.Sprintf("header-%d.json", i+1)), r); err != nil {
			return err
		}
	}
	for i, r := range sc.revokes {
		if err := writeJSON(filepath.Join(sc.dir, fmt.Sprintf("revoke-%d.json", i+1)), r); err != nil {
			return err
		}
	}
	for _, nm := range sc.messages {
		if err := writeJSON(filepath.Join(sc.dir, nm.name+".json"), nm.req); err != nil {
			return err
		}
	}
	playbook := append([]string{"# Ordered playbook (relative request files)"}, sc.steps...)
	return os.WriteFile(filepath.Join(sc.dir, "playbook.txt"),
		[]byte(joinLines(playbook)+"\n"), 0o644)
}

func joinLines(ls []string) string {
	out := ""
	for _, l := range ls {
		out += l + "\n"
	}
	return out
}
