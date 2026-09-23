// 05_deep_capture.slang — capture threading across multiple nesting levels.
// level3 reads a variable two frames up; intermediate frames must thread the
// same cell through their own closure environments.
fn outer(base) {
  let middle = fn() {
    let local = 1000;
    let inner = fn() {
      base = base + 1;          // captured from `outer`, 2 levels up
      return base + local;      // local is captured one level up
    };
    return inner;
  };
  return middle();
}

let f = outer(0);
print(f());
print(f());
print(f());
