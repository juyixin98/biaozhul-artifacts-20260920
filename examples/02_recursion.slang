// 02_recursion.slang — direct and mutual recursion with hoisting.
fn fact(n) {
  if (n <= 1) { return 1; }
  return n * fact(n - 1);
}
print(fact(5));
print(fact(10));

// Recursion through a function expression that names itself.
let fib = fn self(n) {
  if (n < 2) { return n; }
  return self(n - 1) + self(n - 2);
};
print(fib(10));

// Mutual recursion between two hoisted declarations.
fn isEven(n) {
  if (n == 0) { return 1; }
  return isOdd(n - 1);
}
fn isOdd(n) {
  if (n == 0) { return 0; }
  return isEven(n - 1);
}
print(isEven(10));
print(isOdd(7));
