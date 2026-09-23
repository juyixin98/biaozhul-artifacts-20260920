package socks5

import (
	"context"
	"errors"
	"net"
	"testing"
)

func mustAllowList(t *testing.T, cidrs, hosts []string) *AllowList {
	t.Helper()
	wl, err := ParseAllowList(cidrs, hosts)
	if err != nil {
		t.Fatalf("ParseAllowList: %v", err)
	}
	return wl
}

func TestAllowListLiteralIPs(t *testing.T) {
	wl := mustAllowList(t, DefaultAllowCIDRs, DefaultAllowHosts)

	allowed := []string{"127.0.0.1", "127.1.2.3", "::1"}
	for _, s := range allowed {
		if !wl.AddrAllowed(net.ParseIP(s)) {
			t.Errorf("expected %s allowed", s)
		}
	}
	denied := []string{"8.8.8.8", "10.0.0.1", "192.168.1.1", "::2", "0.0.0.0", "224.0.0.1"}
	for _, s := range denied {
		if wl.AddrAllowed(net.ParseIP(s)) {
			t.Errorf("expected %s denied", s)
		}
	}
}

func TestAllowListHostSuffix(t *testing.T) {
	wl := mustAllowList(t, nil, []string{"localhost", ".example.com"})
	cases := map[string]bool{
		"localhost":        true,
		"LocalHost.":       true, // trailing dot / case insensitive
		"a.example.com":    true,
		"b.c.example.com":  true,
		"example.com":      false, // ".example.com" matches subdomains only
		"evil-example.com": false,
		"notexample.com":   false,
	}
	for host, want := range cases {
		if got := wl.HostAllowed(host); got != want {
			t.Errorf("HostAllowed(%q) = %v, want %v", host, got, want)
		}
	}
}

type fakeResolver struct {
	ips map[string][]net.IP
	err error
}

func (f fakeResolver) LookupIP(_ context.Context, _, host string) ([]net.IP, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.ips[host], nil
}

func TestCheckResolveLiteralAndHost(t *testing.T) {
	wl := mustAllowList(t, DefaultAllowCIDRs, DefaultAllowHosts)

	got, err := wl.CheckResolve("127.0.0.1", nil)
	if err != nil || got != "127.0.0.1" {
		t.Fatalf("literal: got %q, %v", got, err)
	}

	// An explicitly listed host is returned without resolution.
	got, err = wl.CheckResolve("localhost", fakeResolver{})
	if err != nil || got != "localhost" {
		t.Fatalf("host rule: got %q, %v", got, err)
	}
}

func TestCheckResolveDeniedLiteral(t *testing.T) {
	wl := mustAllowList(t, DefaultAllowCIDRs, nil)
	_, err := wl.CheckResolve("8.8.8.8", nil)
	if !errors.Is(err, errNotAllowed) {
		t.Fatalf("want errNotAllowed, got %v", err)
	}
}

func TestCheckResolveRebindGuard(t *testing.T) {
	wl := mustAllowList(t, DefaultAllowCIDRs, nil)

	// All resolved IPs allowed -> returns that literal.
	r := fakeResolver{ips: map[string][]net.IP{
		"good.test": {net.ParseIP("127.0.0.1"), net.ParseIP("::1")},
	}}
	got, err := wl.CheckResolve("good.test", r)
	if err != nil {
		t.Fatalf("allowed resolution: %v", err)
	}
	if net.ParseIP(got) == nil {
		t.Fatalf("expected IP literal back, got %q", got)
	}

	// One forbidden IP among the answers must reject the whole request.
	r = fakeResolver{ips: map[string][]net.IP{
		"evil.test": {net.ParseIP("127.0.0.1"), net.ParseIP("1.2.3.4")},
	}}
	if _, err := wl.CheckResolve("evil.test", r); !errors.Is(err, errNotAllowed) {
		t.Fatalf("want errNotAllowed for mixed answers, got %v", err)
	}
}

func TestParseAllowListErrors(t *testing.T) {
	if _, err := ParseAllowList([]string{"not-a-cidr"}, nil); err == nil {
		t.Fatal("expected parse error")
	}
	wl, err := ParseAllowList([]string{"10.0.0.1"}, nil) // bare IP -> /32
	if err != nil {
		t.Fatalf("bare IP: %v", err)
	}
	if !wl.AddrAllowed(net.ParseIP("10.0.0.1")) || wl.AddrAllowed(net.ParseIP("10.0.0.2")) {
		t.Fatal("bare IP should act as /32")
	}
}
