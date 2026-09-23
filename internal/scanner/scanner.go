// Package scanner 实现 C 语言 #include 指令子集的词法扫描器。
//
// 支持的子集：
//   - #include "path"（引号形式）
//   - #include <path>（尖括号形式）
//
// 扫描器会正确跳过：
//   - 行注释 // ... 与块注释 /* ... */（可跨行）中的伪 #include
//   - 字符串字面量 "..." 与字符常量 '...' 中的 #include 文本
//   - 反斜杠换行的行拼接（C 预处理翻译阶段 2 的简化实现）
//
// 明确不支持：
//   - 宏生成的 / 计算型 #include（如 #include FOO、#include(FOO)），
//     遇到时返回一条 error 级别的诊断。
package scanner

import (
	"bytes"
	"fmt"
)

// Kind 表示 include 的形式。
type Kind string

const (
	// Quoted 对应 #include "foo.h"。
	Quoted Kind = "quoted"
	// Angled 对应 #include <foo.h>。
	Angled Kind = "angled"
)

// Include 是一条被识别出的 include 指令。
type Include struct {
	Path string `json:"path"`
	Kind Kind   `json:"kind"`
	// Line 为 1 起始的物理行号（即使经过反斜杠拼接也按源文件实际行号计）。
	Line int `json:"line"`
}

// Severity 为诊断级别。
type Severity string

const (
	// SeverityError 表示不支持或无法解析的指令，扫描结果应判定为失败。
	SeverityError Severity = "error"
	// SeverityWarning 表示可继续扫描但需要关注的问题。
	SeverityWarning Severity = "warning"
)

// Diagnostic 是一条扫描诊断。
type Diagnostic struct {
	Severity Severity `json:"severity"`
	File     string   `json:"file"`
	Line     int      `json:"line,omitempty"`
	Message  string   `json:"message"`
}

var utf8BOM = []byte{0xEF, 0xBB, 0xBF}

type lexState int

const (
	stCode lexState = iota
	stLineComment
	stBlockComment
	stDString
	stSString
)

// logicalLine 是一条经过注释剥离与行拼接后的逻辑行。
type logicalLine struct {
	text      []byte
	startLine int // 该逻辑行在源文件中的起始物理行号（1 起始）
}

// splitLogical 执行简化的预处理：剥离注释、处理 "\" 换行拼接，
// 输出逻辑行序列。注释内容替换为空格，块注释中的换行予以保留，
// 从而不影响行号统计。
func splitLogical(src []byte) []logicalLine {
	if bytes.HasPrefix(src, utf8BOM) {
		src = src[len(utf8BOM):]
	}

	var lines []logicalLine
	var cur bytes.Buffer
	state := stCode
	physLine := 1
	curStart := 1

	flush := func() {
		lines = append(lines, logicalLine{
			text:      append([]byte(nil), cur.Bytes()...),
			startLine: curStart,
		})
		cur.Reset()
	}

	n := len(src)
	for i := 0; i < n; {
		b := src[i]

		// 反斜杠 + 换行：行拼接。代码、字符串、行注释中均适用
		// （C 标准中拼接发生在注释识别之前；块注释内的拼接对本子集无影响，
		// 因此不在 stBlockComment 中处理）。
		if state == stCode || state == stDString || state == stSString || state == stLineComment {
			if b == '\\' && i+1 < n && (src[i+1] == '\n' || src[i+1] == '\r') {
				j := i + 1
				if src[j] == '\r' && j+1 < n && src[j+1] == '\n' {
					j++
				}
				physLine++
				i = j + 1
				continue
			}
		}

		switch state {
		case stCode:
			switch {
			case b == '/' && i+1 < n && src[i+1] == '/':
				state = stLineComment
				cur.WriteByte(' ')
				cur.WriteByte(' ')
				i += 2
			case b == '/' && i+1 < n && src[i+1] == '*':
				state = stBlockComment
				cur.WriteByte(' ')
				cur.WriteByte(' ')
				i += 2
			case b == '"':
				state = stDString
				cur.WriteByte(b)
				i++
			case b == '\'':
				state = stSString
				cur.WriteByte(b)
				i++
			case b == '\n':
				flush()
				physLine++
				curStart = physLine
				i++
			case b == '\r':
				flush()
				if i+1 < n && src[i+1] == '\n' {
					i++
				}
				physLine++
				curStart = physLine
				i++
			default:
				cur.WriteByte(b)
				i++
			}

		case stLineComment:
			switch {
			case b == '\n':
				state = stCode
				flush()
				physLine++
				curStart = physLine
				i++
			case b == '\r':
				state = stCode
				flush()
				if i+1 < n && src[i+1] == '\n' {
					i++
				}
				physLine++
				curStart = physLine
				i++
			default:
				cur.WriteByte(' ')
				i++
			}

		case stBlockComment:
			switch {
			case b == '*' && i+1 < n && src[i+1] == '/':
				state = stCode
				cur.WriteByte(' ')
				cur.WriteByte(' ')
				i += 2
			case b == '\n':
				// 保留换行：块注释不参与行拼接，物理行边界仍在。
				cur.WriteByte('\n')
				flush()
				physLine++
				curStart = physLine
				i++
			case b == '\r':
				cur.WriteByte('\n')
				flush()
				if i+1 < n && src[i+1] == '\n' {
					i++
				}
				physLine++
				curStart = physLine
				i++
			default:
				cur.WriteByte(' ')
				i++
			}

		case stDString:
			switch {
			case b == '\\' && i+1 < n && src[i+1] != '\n' && src[i+1] != '\r':
				// 转义字符原样保留两个字节，避免 \" 提前结束字符串。
				cur.WriteByte(b)
				cur.WriteByte(src[i+1])
				i += 2
			case b == '"':
				state = stCode
				cur.WriteByte(b)
				i++
			case b == '\n':
				// 字符串中出现裸换行属于非法写法，按回到代码态处理。
				state = stCode
				flush()
				physLine++
				curStart = physLine
				i++
			case b == '\r':
				state = stCode
				flush()
				if i+1 < n && src[i+1] == '\n' {
					i++
				}
				physLine++
				curStart = physLine
				i++
			default:
				cur.WriteByte(b)
				i++
			}

		case stSString:
			switch {
			case b == '\\' && i+1 < n && src[i+1] != '\n' && src[i+1] != '\r':
				cur.WriteByte(b)
				cur.WriteByte(src[i+1])
				i += 2
			case b == '\'':
				state = stCode
				cur.WriteByte(b)
				i++
			case b == '\n':
				state = stCode
				flush()
				physLine++
				curStart = physLine
				i++
			case b == '\r':
				state = stCode
				flush()
				if i+1 < n && src[i+1] == '\n' {
					i++
				}
				physLine++
				curStart = physLine
				i++
			default:
				cur.WriteByte(b)
				i++
			}
		}
	}
	flush()

	return lines
}

func isSpace(b byte) bool { return b == ' ' || b == '\t' }

func isIdentByte(b byte) bool {
	return b == '_' || (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// parseIncludeLine 解析单条逻辑行；非 include 指令返回 nil, nil。
func parseIncludeLine(line logicalLine) (*Include, *Diagnostic) {
	s := line.text
	i := 0
	for i < len(s) && isSpace(s[i]) {
		i++
	}
	if i >= len(s) || s[i] != '#' {
		return nil, nil
	}
	i++
	for i < len(s) && isSpace(s[i]) {
		i++
	}
	if !bytes.HasPrefix(s[i:], []byte("include")) {
		return nil, nil
	}
	i += len("include")
	// 排除 #includer、#include_x 之类的误匹配。
	if i < len(s) && isIdentByte(s[i]) {
		return nil, nil
	}
	for i < len(s) && isSpace(s[i]) {
		i++
	}

	mkErr := func(format string, args ...any) *Diagnostic {
		return &Diagnostic{
			Severity: SeverityError,
			Line:     line.startLine,
			Message:  fmt.Sprintf(format, args...),
		}
	}

	if i >= len(s) {
		return nil, mkErr("malformed #include: missing path")
	}

	switch s[i] {
	case '"':
		open := i
		i++
		start := i
		for i < len(s) && s[i] != '"' {
			i++
		}
		if i >= len(s) {
			return nil, mkErr("malformed #include: unclosed quote in %s", bytes.TrimSpace(s[open:]))
		}
		path := string(s[start:i])
		i++ // 跳过闭合引号
		if trail := bytes.TrimSpace(s[i:]); len(trail) > 0 {
			return nil, mkErr("malformed #include: unexpected tokens after path: %q", string(trail))
		}
		if path == "" {
			return nil, mkErr("malformed #include: empty include path")
		}
		return &Include{Path: path, Kind: Quoted, Line: line.startLine}, nil

	case '<':
		open := i
		i++
		start := i
		for i < len(s) && s[i] != '>' {
			i++
		}
		if i >= len(s) {
			return nil, mkErr("malformed #include: unclosed angle bracket in %s", bytes.TrimSpace(s[open:]))
		}
		path := string(s[start:i])
		i++ // 跳过 '>'
		if trail := bytes.TrimSpace(s[i:]); len(trail) > 0 {
			return nil, mkErr("malformed #include: unexpected tokens after path: %q", string(trail))
		}
		if path == "" {
			return nil, mkErr("malformed #include: empty include path")
		}
		return &Include{Path: path, Kind: Angled, Line: line.startLine}, nil

	default:
		snippet := string(bytes.TrimSpace(s[bytes.IndexByte(s, '#'):]))
		if len(snippet) > 80 {
			snippet = snippet[:80] + "..."
		}
		return nil, mkErr("macro-generated/computed #include is not supported: %s", snippet)
	}
}

// Extract 从源文件内容中提取 include 指令。
// filename 仅用于填充诊断信息；返回的 includes 按文件中出现顺序排列。
func Extract(filename string, content []byte) ([]Include, []Diagnostic) {
	var includes []Include
	var diagnostics []Diagnostic
	for _, ll := range splitLogical(content) {
		inc, diag := parseIncludeLine(ll)
		if inc != nil {
			includes = append(includes, *inc)
		}
		if diag != nil {
			diag.File = filename
			diagnostics = append(diagnostics, *diag)
		}
	}
	return includes, diagnostics
}
