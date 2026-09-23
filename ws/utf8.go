package ws

// utf8Validator 是跨分片的增量 UTF-8 校验器。
//
// 校验单位是“整个文本消息”（RFC 6455 §5.6：多字节字符允许跨文本帧分片）。
// 每个分片到来时，把上一分片尾部不足 4 字节的残尾与当前分片拼接后
// 逐字节跑一遍显式 DFA；中间出现任何非法序列立即失败（1007），
// 仅允许在非终结分片边界保留末尾一段不完整的多字节序列。
//
// 不直接使用 unicode/utf8.DecodeRune 的原因：它对代理项等非法 3 字节
// 序列返回 (RuneError, 3)，与合法字符 U+FFFD 无法区分。
type utf8Validator struct {
	residual []byte // 上一个非终结分片尾部不完整序列，长度 0..3（含前导时最多到帧尾）
}

// validate 校验残尾 + frag 拼接后的数据。
// final 为 true 时要求消息完整结束；为 false 时允许末尾挂起不完整序列。
func (v *utf8Validator) validate(frag []byte, final bool) error {
	buf := frag
	if len(v.residual) > 0 {
		buf = make([]byte, 0, len(v.residual)+len(frag))
		buf = append(buf, v.residual...)
		buf = append(buf, frag...)
		v.residual = v.residual[:0]
	}

	i := 0
	for i < len(buf) {
		start := i
		lead := buf[i]
		i++

		// remaining：还需多少个续字节；low/high：紧邻续字节的允许范围。
		var remaining int
		var low, high byte
		switch {
		case lead < 0x80:
			continue // ASCII
		case 0xC2 <= lead && lead <= 0xDF:
			remaining, low, high = 1, 0x80, 0xBF
		case lead == 0xE0:
			remaining, low, high = 2, 0xA0, 0xBF // 禁止 overlong
		case 0xE1 <= lead && lead <= 0xEC:
			remaining, low, high = 2, 0x80, 0xBF
		case lead == 0xED:
			remaining, low, high = 2, 0x80, 0x9F // 禁止 UTF-16 代理项
		case 0xEE <= lead && lead <= 0xEF:
			remaining, low, high = 2, 0x80, 0xBF
		case lead == 0xF0:
			remaining, low, high = 3, 0x90, 0xBF // 禁止 overlong
		case 0xF1 <= lead && lead <= 0xF3:
			remaining, low, high = 3, 0x80, 0xBF
		case lead == 0xF4:
			remaining, low, high = 3, 0x80, 0x8F // 禁止 > U+10FFFF
		default:
			// 0x80-0xBF 游离续字节、0xC0/0xC1（overlong）、0xF5-0xFF 均非法。
			return errInvalidUTF8
		}

		for remaining > 0 {
			if i >= len(buf) {
				// 多字节字符在缓冲边界处被截断。
				if final {
					return errInvalidUTF8
				}
				v.residual = append(v.residual, buf[start:]...)
				return nil
			}
			b := buf[i]
			if b < low || b > high {
				return errInvalidUTF8
			}
			i++
			remaining--
			low, high = 0x80, 0xBF // 只有第一个续字节需要特殊范围
		}
	}

	if final && len(v.residual) > 0 {
		return errInvalidUTF8
	}
	return nil
}

func (v *utf8Validator) reset() { v.residual = v.residual[:0] }

// errInvalidUTF8 是共享的协议错误，关闭码 1007。
var errInvalidUTF8 = protoError(CloseInvalidFramePayloadData, "invalid UTF-8 in text message")
