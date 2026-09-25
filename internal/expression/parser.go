package expression

import (
	"fmt"
	"strings"
)

// Parser 以递归下降方式解析 SPDX 风格表达式子集。
//
// 文法（优先级 OR < AND < WITH/原子）：
//
//	orExpr  := andExpr (OR andExpr)*
//	andExpr := atom (AND atom)*
//	atom    := license (WITH exception)? | '(' orExpr ')'
//
// WITH 只能直接作用于单个许可证，SPDX 规则 "(...) WITH X" 同样不被接受。
type Parser struct {
	tokens []Token
	pos    int
}

// NewParser 构造解析器。
func NewParser(tokens []Token) *Parser {
	return &Parser{tokens: tokens}
}

// Parse 解析并返回 AST 根节点。空输入返回错误。
func (p *Parser) Parse() (Node, error) {
	if p.peek().Type == TokenEOF {
		return nil, fmt.Errorf("位置 %d: 表达式为空", p.peek().Pos)
	}
	node, err := p.parseOr()
	if err != nil {
		return nil, err
	}
	if p.peek().Type != TokenEOF {
		t := p.peek()
		return nil, fmt.Errorf("位置 %d: 表达式解析完成后存在多余记号 %q", t.Pos, t.Lit)
	}
	return node, nil
}

func (p *Parser) parseOr() (Node, error) {
	left, err := p.parseAnd()
	if err != nil {
		return nil, err
	}
	for p.peek().Type == TokenOR {
		p.next()
		right, err := p.parseAnd()
		if err != nil {
			return nil, err
		}
		left = &OrNode{Left: left, Right: right}
	}
	return left, nil
}

func (p *Parser) parseAnd() (Node, error) {
	left, err := p.parseAtom()
	if err != nil {
		return nil, err
	}
	for p.peek().Type == TokenAND {
		p.next()
		right, err := p.parseAtom()
		if err != nil {
			return nil, err
		}
		left = &AndNode{Left: left, Right: right}
	}
	return left, nil
}

func (p *Parser) parseAtom() (Node, error) {
	t := p.peek()
	switch t.Type {
	case TokenLParen:
		p.next()
		inner, err := p.parseOr()
		if err != nil {
			return nil, err
		}
		rt := p.peek()
		if rt.Type != TokenRParen {
			if rt.Type == TokenEOF {
				return nil, fmt.Errorf("位置 %d: 缺少右括号 \")\"", t.Pos)
			}
			return nil, fmt.Errorf("位置 %d: 括号内表达式后存在意外记号 %q，缺少右括号", rt.Pos, rt.Lit)
		}
		p.next()
		return inner, nil
	case TokenIdent:
		return p.parseLicense()
	case TokenAND, TokenOR:
		return nil, fmt.Errorf("位置 %d: 运算符 %q 左侧缺少许可证", t.Pos, t.Lit)
	case TokenWITH:
		return nil, fmt.Errorf("位置 %d: WITH 必须位于许可证之后，不能作为表达式开头", t.Pos)
	case TokenRParen:
		return nil, fmt.Errorf("位置 %d: 意外的右括号 \")\"", t.Pos)
	case TokenEOF:
		return nil, fmt.Errorf("位置 %d: 表达式意外结束，此处缺少许可证或左括号", t.Pos)
	default:
		return nil, fmt.Errorf("位置 %d: 意外的记号 %q", t.Pos, t.Lit)
	}
}

func (p *Parser) parseLicense() (Node, error) {
	t := p.next() // TokenIdent
	lic := &LicenseNode{License: t.Lit}

	if p.peek().Type == TokenWITH {
		wpos := p.peek().Pos
		p.next()
		ex := p.peek()
		switch ex.Type {
		case TokenIdent:
			p.next()
			return &WithNode{License: lic, Exception: ex.Lit}, nil
		case TokenEOF:
			return nil, fmt.Errorf("位置 %d: WITH 之后缺少例外标识符", wpos)
		case TokenLParen:
			return nil, fmt.Errorf("位置 %d: WITH 之后必须是单个例外标识符，不能是括号表达式", ex.Pos)
		default:
			return nil, fmt.Errorf("位置 %d: WITH 之后需要例外标识符，实际为 %q", ex.Pos, ex.Lit)
		}
	}
	return lic, nil
}

func (p *Parser) peek() Token {
	return p.tokens[p.pos]
}

func (p *Parser) next() Token {
	t := p.tokens[p.pos]
	if p.pos < len(p.tokens)-1 {
		p.pos++
	}
	return t
}

// Parse 是便捷函数：一次完成词法与语法分析。
func Parse(input string) (Node, error) {
	tokens, err := NewLexer(input).Tokenize()
	if err != nil {
		return nil, err
	}
	node, err := NewParser(tokens).Parse()
	if err != nil {
		return nil, err
	}
	return node, nil
}

// String 以规范（最小括号、大写运算符）形式渲染 AST。
func String(n Node) string {
	return format(n, 0)
}

const (
	precOr = iota + 1
	precAnd
	precAtom
)

func precOf(n Node) int {
	switch n.(type) {
	case *OrNode:
		return precOr
	case *AndNode:
		return precAnd
	default:
		return precAtom
	}
}

// format 以 parentPrec 为上下文优先级渲染节点，仅在必要处添加括号。
func format(n Node, parentPrec int) string {
	switch v := n.(type) {
	case *LicenseNode:
		return v.License
	case *WithNode:
		return fmt.Sprintf("%s WITH %s", v.License.License, v.Exception)
	case *AndNode:
		s := format(v.Left, precAnd) + " AND " + format(v.Right, precAnd)
		if parentPrec > precAnd {
			s = "(" + s + ")"
		}
		return s
	case *OrNode:
		s := format(v.Left, precOr) + " OR " + format(v.Right, precOr)
		if parentPrec > precOr {
			s = "(" + s + ")"
		}
		return s
	default:
		return ""
	}
}

// Licenses 按从左到右的遍历顺序返回表达式中出现的全部许可证（去重）。
// 同一许可证与不同例外的组合会重复出现。
func Licenses(n Node) []string {
	seen := map[string]bool{}
	var out []string
	var walk func(Node)
	walk = func(x Node) {
		switch v := x.(type) {
		case *LicenseNode:
			if !seen[v.License] {
				seen[v.License] = true
				out = append(out, v.License)
			}
		case *WithNode:
			if !seen[v.License.License] {
				seen[v.License.License] = true
				out = append(out, v.License.License)
			}
		case *AndNode:
			walk(v.Left)
			walk(v.Right)
		case *OrNode:
			walk(v.Left)
			walk(v.Right)
		}
	}
	walk(n)
	return out
}

// ValidKeywords 供文档/错误提示引用。
var ValidKeywords = []string{"AND", "OR", "WITH"}

// QuoteExpr 仅用于统一的错误/日志展示。
func QuoteExpr(s string) string {
	return strings.TrimSpace(s)
}
