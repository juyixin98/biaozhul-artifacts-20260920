package testsite

import "net"

// newListener creates a TCP listener.
func newListener(addr string) (net.Listener, error) {
	return net.Listen("tcp", addr)
}
