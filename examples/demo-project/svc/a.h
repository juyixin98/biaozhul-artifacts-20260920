#ifndef SVC_A_H
#define SVC_A_H

/* Two headers include each other behind guards -> dependency cycle. */
#include "b.h"

void a_step(void);

#endif
