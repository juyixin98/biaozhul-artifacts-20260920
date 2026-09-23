package api

import "encoding/hex"

func b2hex(b []byte) string { return hex.EncodeToString(b) }
