package serverapi

import (
	"io"
	"net"
)

func newListener() (net.Listener, error) {
	return net.Listen("tcp", "127.0.0.1:0")
}

func readAll(r io.Reader) ([]byte, error) { return io.ReadAll(r) }
