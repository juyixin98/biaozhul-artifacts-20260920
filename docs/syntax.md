# Imp — language definition

Imp is a tiny imperative integer language used by this project. Everything
(parsing, analysis, execution) is implemented from scratch in this
repository; no external compiler is used.

## 1. Program structure

A program is a sequence of **declarations** followed by a sequence of
**statements**:

```
program := declaration* statement*
```

There are no block-local declarations; all arrays and scalar variables are
declared at the top.

### Declarations

```
decl := 'input' ID ';'                          # externally supplied scalar
      | 'var'   ID ('=' int_expr)? ';'          # scalar, optionally initialised
      | 'arr'   ID '[' INT ']' '=' '[' INT (',' INT)* ']' ';'
```

* `input n;` declares a scalar whose value is supplied per execution / whose
  range is supplied for analysis. An input with no analysis bound starts at
  the fully unknown interval `[-oo, +oo]`.
* `var x;` / `var x = <integer expression>;`. Initializers may use earlier
  declared scalars/arrays. A compile-time constant is folded; a non-constant
  initializer is sugar for an assignment executed before the program body.
* Arrays have a fixed, positive **integer literal** size and fully specified
  integer-literal initial elements (exactly `size` of them). Array element
  type is integer.

## 2. Types

Only two primitive types exist:

* arbitrary precision **integer** (`Z`, mathematical integers);
* **boolean**, which exists only as a condition / test; it cannot be stored in
  a variable.

Operators are checked: arithmetic over ints, comparisons yield bool, `and`/
`or`/`not` operate on bools.

## 3. Expressions (precedence low → high)

| Level | Operators |
|------|-----------|
| logical or  | <code>\|\|</code> or `or` |
| logical and | `&&` or `and` |
| logical not | `!` or `not` (unary prefix) |
| comparison  | `==` `!=` `<` `<=` `>` `>=` (integers → bool) |
| additive    | `+` `-` |
| multiplicative | `*` `/` `%` |
| unary sign  | unary `-` (and optional `+`) |
| primary     | `INT`, `true`, `false`, `ID`, `ID[expr]`, `(expr)` |

Integer literals are non-negative digit sequences of arbitrary length
(`-x` is the unary minus operator).

### Arithmetic semantics (mathematical integers)

* `+ - *` are the usual integer operations with no overflow.
* `/` is **floor division** and `%` is the corresponding **floor remainder**
  (Python `//` and `%`): `q = a // b`, `r = a - q*b`, with `0 <= r < b` for
  `b > 0` and `b < r <= 0` for `b < 0`. Examples: `-7 / 2 == -4`,
  `-7 % 2 == 1`, `7 / -2 == -4`, `7 % -2 == -1`.
* Division or remainder **by zero is a runtime fault** and is one of the two
  properties the static analyzer reports.

## 4. Statements

```
statement := lvalue '=' expr ';'             # assignment
           | 'if' '(' expr ')' block ('else' block)?
           | 'while' '(' expr ')' block
           | block
block     := '{' statement* '}'
lvalue    := ID | ID '[' expr ']'
```

* Conditions (`if`/`while`) must be boolean expressions.
* Bodies are always brace-delimited (this also removes dangling-else
  ambiguity; `else` binds to the nearest `if`).
* There is deliberately no `break`, `continue`, function call, or I/O in the
  statement language; termination of a `while` loop is governed by its
  condition as written.

### Array indexing

A valid index satisfies `0 <= index < size`. Imp has **no Python-style
negative indexing**: a negative index is an out-of-bounds fault, exactly like
an index `>= size`. Array out-of-bounds (read or write, negative or too
large) is the second property the static analyzer reports.

## 5. Comments and whitespace

* Line comments: `// ...` to end of line.
* Block comments: `/* ... */` (non-nesting).
* Whitespace (spaces, tabs, newlines) is otherwise insignificant.

## 6. Lexical summary

* Identifiers: `[A-Za-z_][A-Za-z0-9_]*`.
* Keywords: `var arr input if else while true false and or not`.
* Operators/punctuation:
  `== != <= >= && || = < > + - * / % ! ( ) { } [ ] ; ,`.
* There is no string or character literal.

## 7. Source locations

Every AST node, every lowered IR instruction, every block and every alarm
records a source span (`line`, `col`, half-open character `[start,end)`).
Lexer/parser/semantic errors and runtime faults therefore point at exact
source coordinates.

## 8. What is out of scope

No frontend, no interactive REPL UI, no type definitions, functions,
pointers, strings, floats, or machine-width integers. The toolchain targets
static interval reasoning about divisions and array bounds over `Z`.
