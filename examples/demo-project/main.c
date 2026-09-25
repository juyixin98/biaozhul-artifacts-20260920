#include <util.h>        /* angled: resolves via includeDirs -> include/util.h */
#include "util.h"        /* quoted: same dir wins -> ./util.h (same-name!) */
#include "net/runner.h"  /* nested quoted path */

/* #include "ghost.h" -- pseudo-include in a comment, must be ignored */

int main(void) {
    return app_double(port_double(1));
}
