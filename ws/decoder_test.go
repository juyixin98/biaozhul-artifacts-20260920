package ws

import (
	"bytes"
	cryptorand "crypto/rand"
	"encoding/binary"
	"errors"
	"math/rand/v2"
	"strings"
	"testing"
)

// ---- 帧构造辅助（测试专用，直接在 bytes.Buffer 上拼 RFC 6455 帧） ----

func maskPayload(payload, key []byte) []byte {
	out := make([]byte, len(payload))
	for i := range payload {
		out[i] = payload[i] ^ key[i%4]
	}
	return out
}

func putFrame(buf *bytes.Buffer, fin bool, opcode int, masked bool, payload []byte) {
	b0 := byte(opcode)
	if fin {
		b0 |= 0x80
	}
	buf.WriteByte(b0)

	maskBit := byte(0)
	if masked {
		maskBit = 0x80
	}
	l := len(payload)
	switch {
	case l <= 125:
		buf.WriteByte(maskBit | byte(l))
	case l <= 0xFFFF:
		buf.WriteByte(maskBit | 126)
		var ext [2]byte
		binary.BigEndian.PutUint16(ext[:], uint16(l))
		buf.Write(ext[:])
	default:
		buf.WriteByte(maskBit | 127)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(l))
		buf.Write(ext[:])
	}
	if masked {
		var key [4]byte
		_, _ = cryptorand.Read(key[:])
		buf.Write(key[:])
		buf.Write(maskPayload(payload, key[:]))
	} else {
		buf.Write(payload)
	}
}

func mustFrame(t *testing.T, fin bool, opcode int, payload []byte) []byte {
	t.Helper()
	var b bytes.Buffer
	putFrame(&b, fin, opcode, true, payload)
	return b.Bytes()
}

func frames(payloads ...[]byte) []byte {
	var b bytes.Buffer
	for _, p := range payloads {
		b.Write(p)
	}
	return b.Bytes()
}

// feedAll 一次性喂入并取事件。
func feedAll(t *testing.T, d *Decoder, data []byte) []Event {
	t.Helper()
	evs, err := d.Feed(data)
	if err != nil {
		t.Fatalf("Feed returned error: %v", err)
	}
	return evs
}

func expectCloseCode(t *testing.T, err error, want int) {
	t.Helper()
	var fe *FrameError
	if !errors.As(err, &fe) {
		t.Fatalf("want FrameError code %d, got %T %v", want, err, err)
	}
	if fe.Code != want {
		t.Fatalf("want close code %d, got %d (%v)", want, fe.Code, fe)
	}
}

// ---- 基础功能 ----

func TestSingleUnmaskedRejected(t *testing.T) {
	// 服务端解码器：客户端帧必须带掩码。
	var b bytes.Buffer
	putFrame(&b, true, OpText, false, []byte("hi"))
	d := NewDecoder()
	_, err := d.Feed(b.Bytes())
	expectCloseCode(t, err, CloseProtocolError)
}

func TestMaskedTextFrame(t *testing.T) {
	d := NewDecoder()
	evs := feedAll(t, d, mustFrame(t, true, OpText, []byte("hello")))
	if len(evs) != 1 || evs[0].Kind != eventMessage || evs[0].OpCode != OpText {
		t.Fatalf("unexpected events: %+v", evs)
	}
	if string(evs[0].Data) != "hello" {
		t.Fatalf("payload = %q", evs[0].Data)
	}
}

func TestMaskedBinaryFrame(t *testing.T) {
	d := NewDecoder()
	payload := []byte{0x00, 0xFF, 0x10, 0x7F}
	evs := feedAll(t, d, mustFrame(t, true, OpBinary, payload))
	if len(evs) != 1 || evs[0].OpCode != OpBinary || !bytes.Equal(evs[0].Data, payload) {
		t.Fatalf("unexpected events: %+v", evs)
	}
}

// ---- 文本/二进制分片重组 ----

func TestFragmentedText(t *testing.T) {
	// "你好" = E4BDA0 E5A5BD，切成 3 份，每帧 2 字节，
	// 除最后一帧外每个多字节字符都跨帧。
	msg := []byte("你好")
	frags := [][]byte{msg[0:2], msg[2:4], msg[4:6]}
	var b bytes.Buffer
	putFrame(&b, false, OpText, true, frags[0])
	putFrame(&b, false, OpContinuation, true, frags[1])
	putFrame(&b, true, OpContinuation, true, frags[2])

	d := NewDecoder()
	evs := feedAll(t, d, b.Bytes())
	if len(evs) != 1 || string(evs[0].Data) != "你好" || evs[0].OpCode != OpText {
		t.Fatalf("unexpected: %+v", evs)
	}
}

func TestFragmentedBinary(t *testing.T) {
	payload := make([]byte, 1000)
	_, _ = cryptorand.Read(payload)
	var b bytes.Buffer
	putFrame(&b, false, OpBinary, true, payload[:300])
	putFrame(&b, false, OpContinuation, true, payload[300:900])
	putFrame(&b, true, OpContinuation, true, payload[900:])
	d := NewDecoder()
	evs := feedAll(t, d, b.Bytes())
	if len(evs) != 1 || !bytes.Equal(evs[0].Data, payload) {
		t.Fatal("binary fragments not reassembled correctly")
	}
}

func TestTwoMessagesBackToBack(t *testing.T) {
	data := frames(
		mustFrame(t, true, OpText, []byte("one")),
		mustFrame(t, true, OpBinary, []byte{1, 2, 3}),
	)
	d := NewDecoder()
	evs := feedAll(t, d, data)
	if len(evs) != 2 || string(evs[0].Data) != "one" ||
		evs[1].OpCode != OpBinary || !bytes.Equal(evs[1].Data, []byte{1, 2, 3}) {
		t.Fatalf("unexpected: %+v", evs)
	}
}

func TestExtendedLength16And64(t *testing.T) {
	for _, n := range []int{126, 65535, 65536, 200000} {
		payload := bytes.Repeat([]byte{'a'}, n)
		d := NewDecoder()
		evs := feedAll(t, d, mustFrame(t, true, OpText, payload))
		if len(evs) != 1 || len(evs[0].Data) != n {
			t.Fatalf("n=%d: unexpected %+v", n, evs)
		}
	}
}

// ---- 控制帧：分片穿插、未分片、超长 ----

func TestControlFrameInterleaved(t *testing.T) {
	var b bytes.Buffer
	putFrame(&b, false, OpText, true, []byte("hel"))
	putFrame(&b, true, OpPing, true, []byte("ping-data"))
	putFrame(&b, false, OpContinuation, true, []byte("lo "))
	putFrame(&b, true, OpPong, true, []byte("pong-data"))
	putFrame(&b, true, OpContinuation, true, []byte("world"))

	d := NewDecoder()
	evs := feedAll(t, d, b.Bytes())
	if len(evs) != 3 {
		t.Fatalf("want 3 events (ping, pong, message), got %+v", evs)
	}
	if evs[0].Kind != eventPing || string(evs[0].Data) != "ping-data" {
		t.Fatalf("ev0 = %+v", evs[0])
	}
	if evs[1].Kind != eventPong || string(evs[1].Data) != "pong-data" {
		t.Fatalf("ev1 = %+v", evs[1])
	}
	if evs[2].Kind != eventMessage || string(evs[2].Data) != "hello world" {
		t.Fatalf("ev2 = %+v", evs[2])
	}
}

func TestFragmentedControlFrameRejected(t *testing.T) {
	var b bytes.Buffer
	putFrame(&b, false, OpPing, true, []byte("x"))
	d := NewDecoder()
	_, err := d.Feed(b.Bytes())
	expectCloseCode(t, err, CloseProtocolError)
}

func TestOversizedControlFrameRejected(t *testing.T) {
	var b bytes.Buffer
	putFrame(&b, true, OpPing, true, make([]byte, 126))
	d := NewDecoder()
	_, err := d.Feed(b.Bytes())
	expectCloseCode(t, err, CloseProtocolError)
}

// ---- 错误的延续帧 / 帧序 ----

func TestContinuationWithoutStart(t *testing.T) {
	d := NewDecoder()
	_, err := d.Feed(mustFrame(t, true, OpContinuation, []byte("x")))
	expectCloseCode(t, err, CloseProtocolError)
}

func TestNewStartBeforeFinish(t *testing.T) {
	var b bytes.Buffer
	putFrame(&b, false, OpText, true, []byte("abc"))
	putFrame(&b, false, OpText, true, []byte("def"))
	d := NewDecoder()
	_, err := d.Feed(b.Bytes())
	expectCloseCode(t, err, CloseProtocolError)
}

func TestUnknownOpcode(t *testing.T) {
	d := NewDecoder()
	_, err := d.Feed(mustFrame(t, true, 0x3, []byte("x")))
	expectCloseCode(t, err, CloseProtocolError)
}

func TestRSVBitsRejected(t *testing.T) {
	var raw []byte
	raw = append(raw, 0xF1, 0x81) // FIN+RSV1+text, masked, len 1
	raw = append(raw, 0, 0, 0, 0)
	raw = append(raw, 0) // mask 0 -> payload 0
	d := NewDecoder()
	_, err := d.Feed(raw)
	expectCloseCode(t, err, CloseProtocolError)
}

// ---- 跨分片 UTF-8 校验：半个多字节字符 ----

func TestHalfMultibyteAcrossFragments(t *testing.T) {
	// '你' = E4 BD A0。
	// 前两个字节放在非终结分片（合法挂起），第三个字节在终结分片补齐。
	good := frames(
		mustFrame(t, false, OpText, []byte{0xE4, 0xBD}),
		mustFrame(t, true, OpContinuation, []byte{0xA0}),
	)
	d := NewDecoder()
	evs := feedAll(t, d, good)
	if len(evs) != 1 || string(evs[0].Data) != "你" {
		t.Fatalf("legit cross-fragment rune failed: %+v", evs)
	}

	// 消息在多字节字符中间结束 -> 1007。
	bad := frames(
		mustFrame(t, false, OpText, []byte("a")),
		mustFrame(t, true, OpContinuation, []byte{0xE4, 0xBD}),
	)
	d = NewDecoder()
	_, err := d.Feed(bad)
	expectCloseCode(t, err, CloseInvalidFramePayloadData)
}

func TestInvalidUTF8InFragment(t *testing.T) {
	// 续字节 0x80 单独出现；即便是非终结分片也不允许。
	bad := mustFrame(t, false, OpText, []byte{0x80})
	d := NewDecoder()
	_, err := d.Feed(bad)
	expectCloseCode(t, err, CloseInvalidFramePayloadData)

	// 前导声明 3 字节，但后面只跟了一个续字节就到帧边界 ->
	// 非终结分片允许挂起；下一个分片给一个非续字节 -> 1007。
	bad2 := frames(
		mustFrame(t, false, OpText, []byte{0xE4, 0xBD}),
		mustFrame(t, false, OpContinuation, []byte{0x41}),
	)
	d = NewDecoder()
	_, err = d.Feed(bad2)
	expectCloseCode(t, err, CloseInvalidFramePayloadData)
}

func TestOverlongAndSurrogateRejected(t *testing.T) {
	cases := [][]byte{
		{0xC0, 0xAF},             // overlong '/'
		{0xED, 0xA0, 0x80},       // UTF-16 surrogate U+D800
		{0xF4, 0x90, 0x80, 0x80}, // > U+10FFFF
	}
	for i, p := range cases {
		d := NewDecoder()
		_, err := d.Feed(mustFrame(t, true, OpText, p))
		if err == nil {
			t.Fatalf("case %d: expected UTF-8 error", i)
		}
		expectCloseCode(t, err, CloseInvalidFramePayloadData)
	}
}

// ---- 大小限制 ----

func TestOversizedMessage(t *testing.T) {
	d := NewDecoder()
	d.MaxMessageSize = 100
	_, err := d.Feed(mustFrame(t, true, OpBinary, make([]byte, 101)))
	expectCloseCode(t, err, CloseMessageTooLarge)
}

func TestOversizedMessageAcrossFragments(t *testing.T) {
	d := NewDecoder()
	d.MaxMessageSize = 100
	var b bytes.Buffer
	putFrame(&b, false, OpBinary, true, make([]byte, 60))
	putFrame(&b, true, OpContinuation, true, make([]byte, 41))
	_, err := d.Feed(b.Bytes())
	expectCloseCode(t, err, CloseMessageTooLarge)
}

func TestOversizedFrame(t *testing.T) {
	d := NewDecoder()
	d.MaxFrameSize = 100
	d.MaxMessageSize = 1 << 20
	_, err := d.Feed(mustFrame(t, true, OpBinary, make([]byte, 101)))
	expectCloseCode(t, err, CloseMessageTooLarge)
}

// ---- 关闭握手 ----

func TestCloseFrameParsing(t *testing.T) {
	cases := []struct {
		name    string
		payload []byte
		code    int
		reason  string
		ok      bool
	}{
		{"empty", nil, 0, "", true},
		{"normal", append([]byte{0x03, 0xE8}, []byte("bye")...), 1000, "bye", true},
		{"one byte", []byte{0x03}, 0, "", false},
		{"bad code 1004", []byte{0x03, 0xEC}, 0, "", false},
		{"bad utf8 reason", []byte{0x03, 0xE8, 0xFF}, 0, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := NewDecoder()
			evs, err := d.Feed(mustFrame(t, true, OpClose, tc.payload))
			if tc.ok {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if len(evs) != 1 || evs[0].Kind != eventClose {
					t.Fatalf("evs = %+v", evs)
				}
				if CloseCode(evs[0].Data) != tc.code || CloseReason(evs[0].Data) != tc.reason {
					t.Fatalf("code=%d reason=%q", CloseCode(evs[0].Data), CloseReason(evs[0].Data))
				}
				// Close 之后再喂数据必须失败。
				if _, err := d.Feed(mustFrame(t, true, OpText, []byte("x"))); !errors.Is(err, ErrClosed) {
					t.Fatalf("post-close feed err = %v, want ErrClosed", err)
				}
			} else if err == nil {
				t.Fatal("expected protocol error")
			}
		})
	}
}

func TestDataAfterCloseRejected(t *testing.T) {
	// Close 帧和一帧数据一起到达：close 事件应吐出，随后报协议错误。
	data := frames(
		mustFrame(t, true, OpClose, nil),
		mustFrame(t, true, OpText, []byte("x")),
	)
	d := NewDecoder()
	evs, err := d.Feed(data)
	if !errors.Is(err, ErrClosed) && err != nil {
		var fe *FrameError
		if !errors.As(err, &fe) {
			t.Fatalf("want FrameError or ErrClosed, got %v", err)
		}
	}
	if len(evs) != 1 || evs[0].Kind != eventClose {
		t.Fatalf("evs = %+v err = %v", evs, err)
	}
}

// ---- 核心验收：随机分块喂入 == 一次性解析 ----

// buildRandomStream 构造包含分片消息、控制帧穿插的合法帧流。
func buildRandomStream(rng *rand.Rand) []byte {
	var b bytes.Buffer
	msgs := []string{
		"hello",
		"你好，world",
		strings.Repeat("abc", 300),
		"", // 空消息
	}
	for mi, msg := range msgs {
		op := OpText
		var raw []byte
		if mi == 2 {
			op = OpBinary
			raw = bytes.Repeat([]byte{0xAB, 0xCD, 0x00}, 300)
		} else {
			raw = []byte(msg)
		}
		if len(raw) <= 1 || rng.IntN(3) == 0 {
			putFrame(&b, true, op, true, raw)
			continue
		}
		// 随机切成 1..5 片。
		chunks := splitRandom(raw, 1+rng.IntN(5), rng)
		for ci, ch := range chunks {
			fin := ci == len(chunks)-1
			if ci == 0 {
				putFrame(&b, fin, op, true, ch)
			} else {
				putFrame(&b, fin, OpContinuation, true, ch)
			}
			// 在非终结分片后随机穿插 ping。
			if !fin && rng.IntN(2) == 0 {
				putFrame(&b, true, OpPing, true, []byte("p"))
			}
		}
	}
	putFrame(&b, true, OpClose, true, []byte{0x03, 0xE8})
	return b.Bytes()
}

func splitRandom(data []byte, parts int, rng *rand.Rand) [][]byte {
	if parts > len(data) {
		parts = len(data)
	}
	if parts == 0 {
		return [][]byte{{}}
	}
	// 随机选 parts-1 个切分点。
	cuts := map[int]bool{0: true, len(data): true}
	for len(cuts) < parts+1 {
		cuts[rng.IntN(len(data)+1)] = true
	}
	points := make([]int, 0, len(cuts))
	for c := range cuts {
		points = append(points, c)
	}
	sortInts(points)
	var out [][]byte
	for i := 1; i < len(points); i++ {
		out = append(out, data[points[i-1]:points[i]])
	}
	return out
}

func sortInts(a []int) {
	for i := 1; i < len(a); i++ {
		for j := i; j > 0 && a[j-1] > a[j]; j-- {
			a[j-1], a[j] = a[j], a[j-1]
		}
	}
}

func TestChunkedEquivalence(t *testing.T) {
	for seed := int64(0); seed < 300; seed++ {
		rng := rand.New(rand.NewPCG(uint64(seed), uint64(seed*7+1)))
		stream := buildRandomStream(rng)

		// 一次性。
		d1 := NewDecoder()
		evsAll, errAll := d1.Feed(stream)

		// 随机分块（含 1 字节分块）。
		d2 := NewDecoder()
		var evsChunked []Event
		var errChunked error
		for off := 0; off < len(stream) && errChunked == nil; {
			step := 1 + rng.IntN(7) // 1..7 字节
			end := off + step
			if end > len(stream) {
				end = len(stream)
			}
			var evs []Event
			evs, errChunked = d2.Feed(stream[off:end])
			evsChunked = append(evsChunked, evs...)
			off = end
		}

		if (errAll == nil) != (errChunked == nil) {
			t.Fatalf("seed=%d: error mismatch all=%v chunked=%v", seed, errAll, errChunked)
		}
		if errAll != nil {
			if errAll.Error() != errChunked.Error() {
				t.Fatalf("seed=%d: different errors %q vs %q", seed, errAll, errChunked)
			}
			continue
		}
		if len(evsAll) != len(evsChunked) {
			t.Fatalf("seed=%d: event count %d vs %d", seed, len(evsAll), len(evsChunked))
		}
		for i := range evsAll {
			a, c := evsAll[i], evsChunked[i]
			if a.Kind != c.Kind || a.OpCode != c.OpCode || !bytes.Equal(a.Data, c.Data) {
				t.Fatalf("seed=%d event %d mismatch:\n all=%+v\n chk=%+v", seed, i, a, c)
			}
		}
	}
}

// 分块方式同样要覆盖“非法流”：构造一个最终会失败的帧流，
// 无论怎么切分，都必须在喂完后得到相同错误。
func TestChunkedEquivalenceInvalid(t *testing.T) {
	streams := [][]byte{
		mustFrame(t, false, OpPing, []byte("x")),
		mustFrame(t, true, OpContinuation, []byte("x")),
		mustFrame(t, true, OpText, []byte{0xE4, 0xBD}), // 半个多字节字符
	}
	for si, stream := range streams {
		for seed := int64(0); seed < 50; seed++ {
			rng := rand.New(rand.NewPCG(uint64(seed), uint64(seed*13+9)))
			d1 := NewDecoder()
			_, errAll := d1.Feed(stream)
			if errAll == nil {
				t.Fatalf("stream %d unexpectedly valid", si)
			}
			d2 := NewDecoder()
			var errChunked error
			for off := 0; off < len(stream); {
				step := 1 + rng.IntN(4)
				end := off + step
				if end > len(stream) {
					end = len(stream)
				}
				_, errChunked = d2.Feed(stream[off:end])
				if errChunked != nil {
					break
				}
				off = end
			}
			if errChunked == nil || errChunked.Error() != errAll.Error() {
				t.Fatalf("stream %d seed %d: %v vs %v", si, seed, errAll, errChunked)
			}
		}
	}
}

// 半个帧头跨 Feed 也必须能补齐。
func TestPartialHeaderAcrossFeeds(t *testing.T) {
	frame := mustFrame(t, true, OpText, []byte("partial"))
	d := NewDecoder()
	var got []Event
	for i := 0; i < len(frame); i++ {
		evs, err := d.Feed(frame[i : i+1])
		if err != nil {
			t.Fatalf("byte %d: %v", i, err)
		}
		got = append(got, evs...)
	}
	if len(got) != 1 || string(got[0].Data) != "partial" {
		t.Fatalf("got %+v", got)
	}
}
