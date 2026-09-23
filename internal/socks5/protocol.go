// Package socks5 implements a loopback-only SOCKS5 CONNECT proxy with
// username/password authentication (RFC 1928, RFC 1929) and a destination
// allow list.
package socks5

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strconv"
)

// SOCKS5 protocol constants (RFC 1928 / RFC 1929).
const (
	Ver5           byte = 0x05
	MethodNoAuth   byte = 0x00 // NO AUTHENTICATION REQUIRED
	MethodUserPass byte = 0x02 // USERNAME/PASSWORD
	MethodNone     byte = 0xFF // NO ACCEPTABLE METHODS

	UserPassVer byte = 0x01
	AuthSuccess byte = 0x00
	AuthFail    byte = 0x01

	CmdConnect byte = 0x01
	CmdBind    byte = 0x02
	CmdUDP     byte = 0x03

	ATypIPv4   byte = 0x01
	ATypDomain byte = 0x03
	ATypIPv6   byte = 0x04

	RepSuccess          byte = 0x00
	RepGeneralFailure   byte = 0x01
	RepNotAllowed       byte = 0x02
	RepNetUnreachable   byte = 0x03
	RepHostUnreachable  byte = 0x04
	RepConnRefused      byte = 0x05
	RepTTLExpired       byte = 0x06
	RepCmdNotSupported  byte = 0x07
	RepAddrNotSupported byte = 0x08
)

var (
	errProtocol   = errors.New("protocol error")
	errAuth       = errors.New("authentication failed")
	errNotAllowed = errors.New("destination not allowed by allowlist")
)

// requestAddress is the DST.ADDR / DST.PORT part of a SOCKS5 CONNECT request.
type requestAddress struct {
	aType byte
	host  string // IP literal (v4/v6) or domain name
	port  uint16
}

// readMethods parses the method selection message:
//
//	+----+----------+----------+
//	|VER | NMETHODS | METHODS  |
//	+----+----------+----------+
//	| 1  |    1     | 1 to 255 |
//	+----+----------+----------+
func readMethods(c byteReader) (methods []byte, err error) {
	ver, err := c.ReadByte()
	if err != nil {
		return nil, err
	}
	if ver != Ver5 {
		return nil, fmt.Errorf("%w: unsupported version 0x%02x", errProtocol, ver)
	}
	nMethods, err := c.ReadByte()
	if err != nil {
		return nil, err
	}
	if nMethods == 0 {
		return nil, fmt.Errorf("%w: client offered zero methods", errProtocol)
	}
	methods = make([]byte, nMethods)
	if _, err := readFull(c, methods); err != nil {
		return nil, err
	}
	return methods, nil
}

// readAuth parses a username/password sub-negotiation message:
//
//	+----+------+----------+------+----------+
//	|VER | ULEN | UNAME    | PLEN | PASSWD   |
//	+----+------+----------+------+----------+
func readAuth(c byteReader) (user, pass string, err error) {
	ver, err := c.ReadByte()
	if err != nil {
		return "", "", err
	}
	if ver != UserPassVer {
		return "", "", fmt.Errorf("%w: unsupported auth version 0x%02x", errProtocol, ver)
	}
	user, err = readLenString(c)
	if err != nil {
		return "", "", err
	}
	pass, err = readLenString(c)
	if err != nil {
		return "", "", err
	}
	return user, pass, nil
}

func readLenString(c byteReader) (string, error) {
	n, err := c.ReadByte()
	if err != nil {
		return "", err
	}
	buf := make([]byte, n)
	if _, err := readFull(c, buf); err != nil {
		return "", err
	}
	return string(buf), nil
}

// readRequest parses the SOCKS5 request and returns its command and target.
//
//	+----+-----+-------+------+----------+----------+
//	|VER | CMD |  RSV  | ATYP | DST.ADDR | DST.PORT |
//	+----+-----+-------+------+----------+----------+
func readRequest(c byteReader) (cmd byte, addr requestAddress, err error) {
	header := make([]byte, 3)
	if _, err := readFull(c, header); err != nil {
		return 0, requestAddress{}, err
	}
	if header[0] != Ver5 {
		return 0, requestAddress{}, fmt.Errorf("%w: unsupported version 0x%02x", errProtocol, header[0])
	}
	if header[2] != 0x00 {
		return 0, requestAddress{}, fmt.Errorf("%w: reserved field is 0x%02x", errProtocol, header[2])
	}
	cmd = header[1]

	aType, err := c.ReadByte()
	if err != nil {
		return 0, requestAddress{}, err
	}
	addr.aType = aType

	switch aType {
	case ATypIPv4:
		buf := make([]byte, 4)
		if _, err := readFull(c, buf); err != nil {
			return 0, requestAddress{}, err
		}
		addr.host = net.IP(buf).String()
	case ATypIPv6:
		buf := make([]byte, 16)
		if _, err := readFull(c, buf); err != nil {
			return 0, requestAddress{}, err
		}
		addr.host = net.IP(buf).String()
	case ATypDomain:
		addr.host, err = readLenString(c)
		if err != nil {
			return 0, requestAddress{}, err
		}
	default:
		return 0, requestAddress{}, fmt.Errorf("%w: unsupported address type 0x%02x", errProtocol, aType)
	}

	portBuf := make([]byte, 2)
	if _, err := readFull(c, portBuf); err != nil {
		return 0, requestAddress{}, err
	}
	addr.port = binary.BigEndian.Uint16(portBuf)
	return cmd, addr, nil
}

func (a requestAddress) portString() string {
	return strconv.Itoa(int(a.port))
}

// byteReader is the subset of *bufio.Reader used by the parsing helpers.
// Reading through a buffered reader is what makes half-packet handshakes work:
// a client that sends one byte at a time is handled transparently.
type byteReader interface {
	ReadByte() (byte, error)
	Read(p []byte) (int, error)
}

func readFull(c byteReader, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := c.Read(buf[total:])
		total += n
		if err != nil {
			return total, err
		}
	}
	return total, nil
}
