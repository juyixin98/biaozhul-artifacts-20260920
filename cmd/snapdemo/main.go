// Command snapdemo is the deterministic demo/acceptance client for snapd.
//
// Demo keys are derived from seed strings via SHA-256 -> Ed25519 and are
// intentionally INSECURE (seed text is the only entropy). They exist so
// examples are reproducible; real deployments use random node/account keys.
package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/example/snapshotprune/internal/crypto"
	"github.com/example/snapshotprune/internal/engine"
	"github.com/example/snapshotprune/internal/types"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	var err error
	switch cmd {
	case "keygen":
		err = keygen(args)
	case "demoinit":
		err = demoInit(args)
	case "genblocks":
		err = genBlocks(args)
	case "transfer":
		err = transfer(args)
	case "replay":
		err = replayFile(args)
	case "lease":
		err = lease(args)
	case "state":
		err = stateCmd(args)
	case "snapshot", "prune", "replay-check":
		err = postCmd(cmd, args)
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `snapdemo - demo/acceptance client for snapd

Usage:
  snapdemo keygen <seed>
      Print private/public key hex and address for a deterministic demo seed.

  snapdemo demoinit <genesis.json>
      Write a reproducible genesis funding demo accounts alice,bob,carol.

  snapdemo genblocks <proposals.jsonl> <count>
      Generate <count> signed block proposals (alice -> bob, rotating) to a file.
      Note: actual nonces depend on on-chain state; use 'transfer' for live runs,
      or feed these to a fresh chain initialized from demoinit in order.

  snapdemo transfer <baseURL> <fromSeed> <toAddress> <amount> <fee>
      Create a lease, read sender nonce, sign one tx, submit the next block.

  snapdemo replay <baseURL> <proposals.jsonl>
      POST every proposal in the JSONL file in order; prints each block hash.

  snapdemo lease <baseURL> <height>
      Create a reader lease; prints JSON (id, height, base, expires_at).

  snapdemo state <baseURL> <leaseID> [address]
      Query state under the lease; optionally focus one account.

  snapdemo snapshot <baseURL> <height>
      Build a full snapshot at height.

  snapdemo prune <baseURL>
      Apply retention (3 snapshots) + delta pruning.

  snapdemo replay-check <baseURL>
      Run independent replay cross-check.
`)
	os.Exit(2)
}

// demoPub returns the Ed25519 public key bytes for a deterministic seed.
func demoPub(seed string) ed25519.PublicKey {
	return crypto.DemoPriv(seed).Public().(ed25519.PublicKey)
}

func keygen(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("keygen needs <seed>")
	}
	k := crypto.DemoPriv(args[0])
	pub := k.Public().(ed25519.PublicKey)
	addr, err := crypto.AddressFromPub(pub)
	if err != nil {
		return err
	}
	out := map[string]string{
		"seed":    args[0],
		"privkey": fmt.Sprintf("%x", []byte(k)),
		"pubkey":  fmt.Sprintf("%x", pub),
		"address": addr.Hex(),
	}
	return printJSON(out)
}

func demoInit(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("demoinit needs <genesis.json>")
	}
	g := types.Genesis{ChainID: "demo-1"}
	for _, seed := range []string{"alice", "bob", "carol"} {
		addr, err := crypto.AddressFromPub(demoPub(seed))
		if err != nil {
			return err
		}
		g.Allocations = append(g.Allocations, types.GenesisAlloc{
			Address: addr.Hex(),
			Balance: 1_000_000,
		})
	}
	b, err := json.MarshalIndent(g, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(args[0], b, 0o644); err != nil {
		return err
	}
	fmt.Println("wrote", args[0])
	return printJSON(g)
}

func httpDo(method, url string, body any, out any) error {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("%s %s -> %s: %s", method, url, resp.Status, strings.TrimSpace(string(data)))
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	fmt.Println(string(data))
	return nil
}

func transfer(args []string) error {
	if len(args) != 5 {
		return fmt.Errorf("transfer needs <baseURL> <fromSeed> <toAddress> <amount> <fee>")
	}
	base, seed, to := args[0], args[1], args[2]
	amount, err := strconv.ParseUint(args[3], 10, 64)
	if err != nil {
		return err
	}
	fee, err := strconv.ParseUint(args[4], 10, 64)
	if err != nil {
		return err
	}
	toAddr, err := types.ParseAddress(to)
	if err != nil {
		return err
	}
	priv := crypto.DemoPriv(seed)
	fromAddr, err := crypto.AddressFromPub(priv.Public().(ed25519.PublicKey))
	if err != nil {
		return err
	}

	// Find tip and create a lease there to read the current nonce.
	var info struct {
		Tip uint64 `json:"tip"`
	}
	if err := httpDo("GET", base+"/v1/info", nil, &info); err != nil {
		return err
	}
	var lease struct {
		ID string `json:"id"`
	}
	if err := httpDo("POST", base+"/v1/leases", map[string]uint64{"height": info.Tip}, &lease); err != nil {
		return err
	}
	defer httpDo("DELETE", base+"/v1/leases/"+lease.ID, nil, nil)

	var qr struct {
		View struct {
			Account *types.Account `json:"account"`
		} `json:"view"`
	}
	if err := httpDo("GET",
		fmt.Sprintf("%s/v1/state?lease=%s&account=%s", base, lease.ID, fromAddr.Hex()),
		nil, &qr); err != nil {
		return err
	}
	var nonce uint64
	if qr.View.Account != nil {
		nonce = qr.View.Account.Nonce
	}

	tx := types.Tx{Nonce: nonce, To: toAddr, Amount: amount, Fee: fee}
	if err := crypto.SignTx(&tx, priv); err != nil {
		return err
	}
	prop := engine.Proposal{Height: info.Tip + 1, Timestamp: time.Now().UnixNano(), Txs: []types.Tx{tx}}
	var result map[string]any
	if err := httpDo("POST", base+"/v1/blocks", prop, &result); err != nil {
		return err
	}
	return printJSON(result)
}

func genBlocks(args []string) error {
	if len(args) != 2 {
		return fmt.Errorf("genblocks needs <proposals.jsonl> <count>")
	}
	n, err := strconv.Atoi(args[1])
	if err != nil {
		return err
	}
	alice := crypto.DemoPriv("alice")
	bob, _ := crypto.AddressFromPub(demoPub("bob"))
	carol, _ := crypto.AddressFromPub(demoPub("carol"))

	f, err := os.Create(args[0])
	if err != nil {
		return err
	}
	defer f.Close()
	for i := 1; i <= n; i++ {
		to := bob
		if i%2 == 0 {
			to = carol
		}
		tx := types.Tx{Nonce: uint64(i - 1), To: to, Amount: 100, Fee: 1}
		if err := crypto.SignTx(&tx, alice); err != nil {
			return err
		}
		p := engine.Proposal{Height: uint64(i), Txs: []types.Tx{tx}}
		b, _ := json.Marshal(p)
		if _, err := f.Write(append(b, '\n')); err != nil {
			return err
		}
	}
	fmt.Println("wrote", n, "signed proposals to", args[0])
	return nil
}

func replayFile(args []string) error {
	if len(args) != 2 {
		return fmt.Errorf("replay needs <baseURL> <proposals.jsonl>")
	}
	base := args[0]
	data, err := os.ReadFile(args[1])
	if err != nil {
		return err
	}
	lines := bytes.Split(data, []byte("\n"))
	n := 0
	for _, line := range lines {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var p engine.Proposal
		if err := json.Unmarshal(line, &p); err != nil {
			return fmt.Errorf("parse proposal: %w", err)
		}
		var res map[string]any
		if err := httpDo("POST", base+"/v1/blocks", &p, &res); err != nil {
			return fmt.Errorf("height %d: %w", p.Height, err)
		}
		n++
		fmt.Printf("block %d ok: %v\n", p.Height, res["hash"])
	}
	fmt.Println("replayed", n, "blocks")
	return nil
}

func lease(args []string) error {
	if len(args) != 2 {
		return fmt.Errorf("lease needs <baseURL> <height>")
	}
	h, err := strconv.ParseUint(args[1], 10, 64)
	if err != nil {
		return err
	}
	var l map[string]any
	if err := httpDo("POST", args[0]+"/v1/leases", map[string]uint64{"height": h}, &l); err != nil {
		return err
	}
	return printJSON(l)
}

func stateCmd(args []string) error {
	if len(args) < 2 {
		return fmt.Errorf("state needs <baseURL> <leaseID> [address]")
	}
	url := fmt.Sprintf("%s/v1/state?lease=%s", args[0], args[1])
	if len(args) == 3 {
		url += "&account=" + args[2]
	}
	return httpDo("GET", url, nil, nil)
}

func postCmd(cmd string, args []string) error {
	switch cmd {
	case "snapshot":
		if len(args) != 2 {
			return fmt.Errorf("snapshot needs <baseURL> <height>")
		}
		h, err := strconv.ParseUint(args[1], 10, 64)
		if err != nil {
			return err
		}
		return httpDo("POST", args[0]+"/v1/snapshots", map[string]uint64{"height": h}, nil)
	case "prune":
		if len(args) != 1 {
			return fmt.Errorf("prune needs <baseURL>")
		}
		return httpDo("POST", args[0]+"/v1/prune", nil, nil)
	case "replay-check":
		if len(args) != 1 {
			return fmt.Errorf("replay-check needs <baseURL>")
		}
		return httpDo("POST", args[0]+"/v1/replay", nil, nil)
	}
	return nil
}

func printJSON(v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(b))
	return nil
}
