#ifndef SVC_B_H
#define SVC_B_H

#include "a.h"

// Pseudo-include inside a line comment must be ignored:
// #include "does-not-exist-comment.h"

/* Pseudo-include inside a block comment:
 * #include <also-not-real.h>
 */

void b_step(void);

#endif
