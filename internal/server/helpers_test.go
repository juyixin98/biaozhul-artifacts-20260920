package server

import (
	"bytes"
	"log"
)

func testLogger(buf *bytes.Buffer) *log.Logger {
	return log.New(buf, "", 0)
}
