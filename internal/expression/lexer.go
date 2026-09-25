package expression

import (
	"fmt"
	"unicode"
)

// Lexer 把表达式字符串切分为 Token。
type Lexer struct {
	input []rune
	pos   int
}

// NewLexer 构造词法分析器。
func NewLexer(input string) *Lexer {
	return &Lexer{input: []rune(input)}
}

// Tokenize 返回全部记号（包含末尾 TokenEOF），遇到非法字符时返回错误。
func (l *Lexer) Tokenize() ([]Token, error) {
	var tokens []Token
	for {
		l.skipSpaces()
		if l.pos >= len(l.input) {
			tokens = append(tokens, Token{Type: TokenEOF, Pos: l.bytePos()})
			return tokens, nil
		}
		start := l.bytePos()
		r := l.input[l.pos]

		switch {
		case r == '(':
			l.pos++
			tokens = append(tokens, Token{Type: TokenLParen, Lit: "(", Pos: start})
		case r == ')':
			l.pos++
			tokens = append(tokens, Token{Type: TokenRParen, Lit: ")", Pos: start})
		case isIdentStart(r):
			tok, err := l.readIdent(start)
			if err != nil {
				return nil, err
			}
			tokens = append(tokens, tok)
		case r == '+':
			return nil, fmt.Errorf("位置 %d: 不支持 SPDX 的 \"+\" 后缀运算符（本子集不实现）", start)
		default:
			return nil, fmt.Errorf("位置 %d: 非法字符 %q", start, string(r))
		}
	}
}

func (l *Lexer) skipSpaces() {
	for l.pos < len(l.input) && unicode.IsSpace(l.input[l.pos]) {
		l.pos++
	}
}

// readIdent 读取一个标识符或关键字。标识符字符集：
// 字母/数字、点、连字符（标识符不得以连字符开头）、冒号（用于
// DocumentRef:...:LicenseRef...）。
func (l *Lexer) readIdent(startByte int) (Token, error) {
	begin := l.pos
	for l.pos < len(l.input) {
		r := l.input[l.pos]
		if !isIdentPart(r) {
			break
		}
		l.pos++
	}
	lit := string(l.input[begin:l.pos])
	tok := Token{Type: TokenIdent, Lit: lit, Pos: startByte}
	switch lit {
	case "AND":
		tok.Type = TokenAND
	case "OR":
		tok.Type = TokenOR
	case "WITH":
		tok.Type = TokenWITH
	}
	// 冒号是 Ref 语法的一部分；单独校验冒号结构，给出清晰错误。
	if err := validateIdent(lit, startByte); err != nil {
		return Token{}, err
	}
	return tok, nil
}

// bytePos 返回当前 rune 位置对应的字节偏移。
func (l *Lexer) bytePos() int {
	return len(string(l.input[:l.pos]))
}

func isIdentStart(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r)
}

func isIdentPart(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '.' || r == '-' || r == ':'
}

// validateIdent 对标识符中的冒号做 SPDX Annex 4 风格的基本校验：
// 冒号不得出现在首尾，也不得相邻。
func validateIdent(lit string, pos int) error {
	for i := 0; i < len(lit); i++ {
		if lit[i] == ':' {
			if i == 0 || i == len(lit)-1 {
				return fmt.Errorf("位置 %d: 标识符 %q 中的冒号位置非法", pos, lit)
			}
			if lit[i-1] == ':' || lit[i+1] == ':' {
				return fmt.Errorf("位置 %d: 标识符 %q 包含相邻冒号", pos, lit)
			}
		}
	}
	return nil
}
