// 03_escape.slang — escaping closures outlive their creating activation.
// `make_counter` returns AFTER its frame would be popped; the incrementer
// must still read and update the heap-allocated count cell.
fn make_counter(start) {
  let step = 2;
  let bump = fn() {
    start = start + step;
    return start;
  };
  return bump;
}

let a = make_counter(0);
let b = make_counter(100);
print(a());
print(a());
print(b());
print(a());
print(b());
