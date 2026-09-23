// 01_shadow.slang — lexical scoping and shadowing.
// A nested block and a nested parameter may bind the same name without
// touching the outer binding; writes inside a shadowing scope must not leak.
let x = 1;
print(x);
{
  let x = 2;
  print(x);
  x = 20;
  print(x);
}
print(x);

let pick = fn(x) {
  // parameter shadows the outer x
  return x + 100;
};
print(pick(7));
print(x);
