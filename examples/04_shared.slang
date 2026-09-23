// 04_shared.slang — multiple closures share one mutable captured binding.
// inc and dec observe each other's updates; the cell is shared, not copied.
fn make_pair() {
  let n = 0;
  let inc = fn() { n = n + 1; return n; };
  let dec = fn() { n = n - 1; return n; };
  let get = fn() { return n; };
  return fn(sel) {
    if (sel == 0) { return inc(); }
    if (sel == 1) { return dec(); }
    return get();
  };
}

let p = make_pair();
print(p(0));
print(p(0));
print(p(1));
print(p(2));

// The captured parameter is shared too.
fn adder(x) {
  return fn(y) { x = x + y; return x; };
}
let add = adder(10);
print(add(5));
print(add(7));
