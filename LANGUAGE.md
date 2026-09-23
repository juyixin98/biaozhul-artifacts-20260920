# Mini language reference

The toolchain compiles a tiny C-like integer language. Everything is a
signed arbitrary-precision integer (Python `int`); booleans are the
integers `0` / `1`. There are no pointers, arrays, structs, globals, or
strings — the surface is deliberately small so the whole pipeline stays
about the SSA construction.

## Program structure

```
program   := func*
func      := 'func' IDENT '(' [IDENT (',' IDENT)*]? ')' block
block     := '{' stmt* '}'
```

A program is one or more named functions. The JSON service executes
`main` by default. Functions take only integer parameters and return one
integer; falling off the end of `main` returns `0`.

## Statements

```
stmt := 'var'    IDENT '=' expr ';'          // declaration with initializer
      | IDENT    '=' expr ';'                // assignment
      | 'if' '(' expr ')' block ('else' (block | if-stmt))?
      | 'while' '(' expr ')' block
      | 'return' [expr]? ';'
      | block
      | expr ';'
```

* Variables are **function-scoped**: a `var` anywhere in a function is
  visible for the rest of that function, including nested blocks. A
  name must be declared once and before it is used textually; duplicate
  declarations and use-before-declaration are compile errors.
* Every slot is logically **zero-initialised at function entry**; the
  mandatory initialiser makes the first value explicit. The zero-init
  matters only on edges where a phi can see a declaration whose
  initialiser has not executed (e.g. a variable declared in a loop body
  observed by the loop header on the entry edge).
* `print(expr);` is a built-in statement-expression: it evaluates and
  records one integer (captured in the interpreter output) and evaluates
  to `0`.
* Statements following a terminating `return` in the same statement list
  are unreachable and dropped during lowering.

## Expressions

Precedence, tightest last:

| level | operators                        | associativity |
|------:|----------------------------------|---------------|
| 1 | `\|\|`                               | left          |
| 2 | `&&`                                 | left          |
| 3 | `\|` (bitwise or)                    | left          |
| 4 | `^`                                  | left          |
| 5 | `&` (bitwise and)                    | left          |
| 6 | `==` `!=`                            | left          |
| 7 | `<` `<=` `>` `>=`                    | left          |
| 8 | `<<` `>>`                            | left          |
| 9 | `+` `-`                              | left          |
| 10 | `*` `/` `%`                         | left          |
| unary | `-` (negate) `!` (logical not) `~` (bitwise not) | prefix |

Literals: decimal integers and `0x`-prefixed hex; `true` / `false`
(`1` / `0`). Parentheses group. Function calls are `IDENT(expr, ...)` and
may appear in expression position; the only built-in other than user
functions is `print(x)`.

`&&` and `||` are **short-circuiting** and are lowered to control flow
through a compiler-generated slot (`__scN__`, reserved name prefix `__`).

Integer division truncates toward zero; `%` follows the same rule and has
the sign of the dividend (`-7 / 2 == -3`, `-7 % 2 == -1`).

## Comments / source positions

`// line comments` and `/* block comments */` are supported. Every token
records byte offset plus 1-based line/column; AST nodes and the IR
instructions lowered from them keep a `loc` pointing at the original
span, so diagnostics name the source line even after SSA conversion.

## Example

```
func abs(x) {
  if (x < 0) { return 0 - x; } else { return x; }
}

func main(n) {
  var i = 0;
  var sum = 0;
  while (i < n) {
    sum = sum + abs(i);
    i = i + 1;
  }
  return sum;
}
```
