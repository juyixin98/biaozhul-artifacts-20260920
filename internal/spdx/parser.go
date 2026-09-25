// Package spdx parses a subset of SPDX license expressions.
//
// Supported grammar (SPDX appendix IV subset):
//
//	expression ::= or-expression
//	or-expression  ::= and-expression ("OR" and-expression)*
//	and-expression ::= with-expression ("AND" with-expression)*
//	with-expression ::= primary ("WITH" exception-id)?
//	primary   ::= license-id | "(" expression ")"
//
// Precedence, lowest to highest: OR < AND < WITH.
// WITH is only valid on a single license id, never on a parenthesised
// group (matching the SPDX grammar). Operators are case-insensitive,
// license and exception identifiers are matched as-is.
package spdx

import (
	"fmt"
	"strings"
	"unicode"
)

// Node is any node of a parsed expression tree.
type Node interface {
	String() string
	node()
}

// License is a leaf, optionally carrying a WITH exception.
type License struct {
	ID        string
	Exception string // empty when there is no WITH clause
}

func (License) node() {}
func (l License) String() string {
	if l.Exception == "" {
		return l.ID
	}
	return l.ID + " WITH " + l.Exception
}

// Op is a binary operator.
type Op string

const (
	AND Op = "AND"
	OR  Op = "OR"
)

// Binary is an AND/OR node. Parse produces a left-associative tree:
// "A OR B OR C" => ((A OR B) OR C).
type Binary struct {
	Op    Op
	Left  Node
	Right Node
}

func (Binary) node()            {}
func (b Binary) String() string { return Print(b) }

// --- parser ---

type tokenKind int

const (
	tkIdent tokenKind = iota
	tkAND
	tkOR
	tkWITH
	tkLParen
	tkRParen
)

type token struct {
	kind tokenKind
	text string
	pos  int
}

type parser struct {
	tokens []token
	i      int
}

// Parse parses an SPDX expression subset. It returns an error on empty
// input, trailing input, missing operands, or WITH applied to a group.
func Parse(input string) (Node, error) {
	toks, err := lex(input)
	if err != nil {
		return nil, err
	}
	p := &parser{tokens: toks}
	node, err := p.parseOr()
	if err != nil {
		return nil, err
	}
	if p.i != len(p.tokens) {
		t := p.tokens[p.i]
		return nil, fmt.Errorf("unexpected token %q at position %d", t.text, t.pos)
	}
	return node, nil
}

func lex(input string) ([]token, error) {
	var toks []token
	runes := []rune(input)
	i := 0
	for i < len(runes) {
		r := runes[i]
		switch {
		case unicode.IsSpace(r):
			i++
		case r == '(':
			toks = append(toks, token{tkLParen, "(", i})
			i++
		case r == ')':
			toks = append(toks, token{tkRParen, ")", i})
			i++
		default:
			start := i
			for i < len(runes) && !unicode.IsSpace(runes[i]) && runes[i] != '(' && runes[i] != ')' {
				i++
			}
			word := string(runes[start:i])
			switch strings.ToUpper(word) {
			case "AND":
				toks = append(toks, token{tkAND, word, start})
			case "OR":
				toks = append(toks, token{tkOR, word, start})
			case "WITH":
				toks = append(toks, token{tkWITH, word, start})
			default:
				toks = append(toks, token{tkIdent, word, start})
			}
		}
	}
	return toks, nil
}

func (p *parser) peek() *token {
	if p.i < len(p.tokens) {
		return &p.tokens[p.i]
	}
	return nil
}

func (p *parser) parseOr() (Node, error) {
	left, err := p.parseAnd()
	if err != nil {
		return nil, err
	}
	for t := p.peek(); t != nil && t.kind == tkOR; t = p.peek() {
		p.i++
		right, err := p.parseAnd()
		if err != nil {
			return nil, err
		}
		left = Binary{Op: OR, Left: left, Right: right}
	}
	return left, nil
}

func (p *parser) parseAnd() (Node, error) {
	left, err := p.parseWith()
	if err != nil {
		return nil, err
	}
	for t := p.peek(); t != nil && t.kind == tkAND; t = p.peek() {
		p.i++
		right, err := p.parseWith()
		if err != nil {
			return nil, err
		}
		left = Binary{Op: AND, Left: left, Right: right}
	}
	return left, nil
}

func (p *parser) parseWith() (Node, error) {
	node, err := p.parsePrimary()
	if err != nil {
		return nil, err
	}
	if t := p.peek(); t != nil && t.kind == tkWITH {
		lic, ok := node.(License)
		if !ok {
			return nil, fmt.Errorf("WITH at position %d can only follow a single license, not a parenthesised group", t.pos)
		}
		p.i++
		exc, err := p.expectIdent("exception identifier after WITH")
		if err != nil {
			return nil, err
		}
		lic.Exception = exc
		return lic, nil
	}
	return node, nil
}

func (p *parser) parsePrimary() (Node, error) {
	t := p.peek()
	if t == nil {
		return nil, fmt.Errorf("expected license identifier but reached end of expression")
	}
	switch t.kind {
	case tkLParen:
		p.i++
		node, err := p.parseOr()
		if err != nil {
			return nil, err
		}
		ct := p.peek()
		if ct == nil {
			return nil, fmt.Errorf("missing closing parenthesis for '(' at position %d", t.pos)
		}
		if ct.kind != tkRParen {
			return nil, fmt.Errorf("expected ')' at position %d but got %q", ct.pos, ct.text)
		}
		p.i++
		return node, nil
	case tkIdent:
		p.i++
		return License{ID: t.text}, nil
	case tkRParen:
		return nil, fmt.Errorf("unexpected ')' at position %d", t.pos)
	default:
		return nil, fmt.Errorf("expected license identifier at position %d but got %q", t.pos, t.text)
	}
}

func (p *parser) expectIdent(what string) (string, error) {
	t := p.peek()
	if t == nil {
		return "", fmt.Errorf("expected %s but reached end of expression", what)
	}
	if t.kind != tkIdent {
		return "", fmt.Errorf("expected %s at position %d but got %q", what, t.pos, t.text)
	}
	p.i++
	return t.text, nil
}

// Print renders a node back to canonical text, inserting parentheses only
// where precedence requires them (OR < AND < WITH, WITH is never grouped).
func Print(n Node) string {
	var b strings.Builder
	write(&b, n, 0)
	return b.String()
}

func write(b *strings.Builder, n Node, parentPrec int) {
	switch v := n.(type) {
	case License:
		b.WriteString(v.String())
	case Binary:
		prec := 0
		if v.Op == AND {
			prec = 1
		}
		if prec < parentPrec {
			b.WriteString("(")
		}
		write(b, v.Left, prec)
		b.WriteString(" ")
		b.WriteString(string(v.Op))
		b.WriteString(" ")
		// AND/OR are associative, so a same-precedence right child needs no
		// parens; a lower-precedence one does ("A AND (B OR C)").
		write(b, v.Right, prec)
		if prec < parentPrec {
			b.WriteString(")")
		}
	}
}
