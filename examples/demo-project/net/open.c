#include "util.h"        /* same-name header: this must resolve to net/util.h */
#include "runner.h"      /* same-dir quoted include */

void net_open(void) { (void)net_ident(0); }
