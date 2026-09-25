// Package expression 实现 SPDX 许可证表达式的一个子集的词法分析。
//
// 支持的记号：
//   - 标识符（许可证 ID / LicenseRef / DocumentRef:LicenseRef）
//   - 关键字 AND、OR、WITH（大小写敏感）
//   - 左右括号
//
// 显式不支持 SPDX 的 "+" 后缀运算符，遇到时返回位置明确的错误。
package expression

// TokenType 表示词法记号的种类。
type TokenType int

const (
	TokenEOF TokenType = iota
	TokenIdent
	TokenAND
	TokenOR
	TokenWITH
	TokenLParen
	TokenRParen
	TokenIllegal
)

// Token 是一个词法记号。Pos 为基于 0 的字节偏移。
type Token struct {
	Type TokenType
	Lit  string
	Pos  int
}
