package socks5

import (
	"context"
	"net"
	"net/netip"
	"strconv"
	"strings"
)

// AllowList restricts which destinations a CONNECT request may reach.
//
// Rules are checked in this order:
//  1. An IP destination must match one of ipNets (loopback is always
//     permitted) and must not be an unspecified/broadcast-style address.
//  2. A domain destination may match a host suffix exactly (".example.com"
//     matches "a.example.com" but not "example.com"; "example.com" matches
//     the bare host too).
//  3. Otherwise the domain is resolved and every resolved IP must match.
type AllowList struct {
	ipNets []netip.Prefix
	hosts  []string // suffix rules as parsed from the CLI
}

// DefaultAllowCIDRs is the local-test destination set: IPv4 + IPv6 loopback
// and the IPv4 link-local / documentation ranges commonly used in tests.
var DefaultAllowCIDRs = []string{
	"127.0.0.0/8",
	"::1/128",
	"169.254.0.0/16",
}

// DefaultAllowHosts covers loopback names and a reserved example suffix.
var DefaultAllowHosts = []string{
	"localhost",
	".localhost",
	".example.com",
}

// ParseAllowList builds an AllowList from CIDR/IP and host-suffix strings.
// An invalid entry returns an error so bad configuration never fails open.
func ParseAllowList(cidrs, hosts []string) (*AllowList, error) {
	wl := &AllowList{hosts: hosts}
	for _, s := range cidrs {
		prefix, err := netip.ParsePrefix(s)
		if err != nil {
			// Accept a bare IP and treat it as a /32 or /128.
			addr, addrErr := netip.ParseAddr(s)
			if addrErr != nil {
				return nil, &AllowListParseError{Entry: s, Err: err}
			}
			prefix = netip.PrefixFrom(addr, addr.BitLen())
		}
		wl.ipNets = append(wl.ipNets, prefix.Masked())
	}
	return wl, nil
}

// AllowListParseError identifies a malformed allowlist entry.
type AllowListParseError struct {
	Entry string
	Err   error
}

func (e *AllowListParseError) Error() string {
	return "invalid allowlist entry " + strconv.Quote(e.Entry) + ": " + e.Err.Error()
}

// HostAllowed reports whether a domain name matches an explicit host rule.
func (w *AllowList) HostAllowed(host string) bool {
	h := strings.ToLower(strings.TrimSuffix(host, "."))
	for _, rule := range w.hosts {
		r := strings.ToLower(strings.TrimSuffix(rule, "."))
		if h == r {
			return true
		}
		if strings.HasPrefix(r, ".") && strings.HasSuffix(h, r) {
			return true
		}
	}
	return false
}

// AddrAllowed reports whether a literal IP destination is permitted.
func (w *AllowList) AddrAllowed(ip net.IP) bool {
	addr, ok := netip.AddrFromSlice(ip)
	if !ok {
		return false
	}
	// net.IP stores IPv4 as a 16-byte v4-mapped address; unmap before
	// comparing against IPv4 prefixes (Prefix.Contains does not do this).
	addr = addr.Unmap()
	if addr.IsUnspecified() || addr.IsMulticast() {
		return false
	}
	for _, prefix := range w.ipNets {
		if prefix.Contains(addr) {
			return true
		}
	}
	return false
}

// IPSResolver resolves host names to IP addresses. *net.Resolver satisfies
// it; tests substitute a deterministic fake.
type IPSResolver interface {
	LookupIP(ctx context.Context, network, host string) ([]net.IP, error)
}

// CheckResolve validates a destination before dialing. For domains with an
// explicit host rule it returns the host unchanged. Other domains are
// resolved and every returned IP must pass the IP rules; it then returns one
// allowed IP literal, which makes the subsequent dial deterministic and stops
// a name from re-resolving to a forbidden address (DNS rebinding).
func (w *AllowList) CheckResolve(host string, resolver IPSResolver) (string, error) {
	if ip := net.ParseIP(host); ip != nil {
		if !w.AddrAllowed(ip) {
			return "", errNotAllowed
		}
		return host, nil
	}
	if w.HostAllowed(host) {
		return host, nil
	}

	ips, err := resolver.LookupIP(context.Background(), "ip", host)
	if err != nil {
		return "", err
	}
	if len(ips) == 0 {
		return "", &net.DNSError{Err: "no resolved addresses", Name: host, IsNotFound: true}
	}
	for _, ip := range ips {
		if !w.AddrAllowed(ip) {
			return "", errNotAllowed
		}
	}
	return ips[0].String(), nil
}
