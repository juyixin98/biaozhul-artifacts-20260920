package com.example.intervals.model;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CutTest {

    @Test
    void orderingAroundAValueIsDense() {
        Cut<Integer> negInf = Cut.negInfinity();
        Cut<Integer> below2 = Cut.below(2);
        Cut<Integer> above2 = Cut.above(2);
        Cut<Integer> posInf = Cut.posInfinity();

        // -inf < BELOW(2) < ABOVE(2) < +inf
        assertTrue(negInf.compareTo(below2) < 0);
        assertTrue(below2.compareTo(above2) < 0);
        assertTrue(above2.compareTo(posInf) < 0);
    }

    @Test
    void ordersByValueThenKind() {
        assertTrue(Cut.<Integer>below(1).compareTo(Cut.below(2)) < 0);
        assertTrue(Cut.<Integer>above(1).compareTo(Cut.below(2)) < 0);
        assertTrue(Cut.<Integer>below(2).compareTo(Cut.above(1)) > 0);
        // same value, same kind
        assertEquals(0, Cut.below(2).compareTo(Cut.below(2)));
    }

    @Test
    void infinitiesAreExtremalAcrossValues() {
        assertTrue(Cut.<Integer>negInfinity().compareTo(Cut.above(1000)) < 0);
        assertTrue(Cut.<Integer>posInfinity().compareTo(Cut.below(-1000)) > 0);
        assertEquals(0, Cut.<Integer>negInfinity().compareTo(Cut.negInfinity()));
        assertEquals(0, Cut.<Integer>posInfinity().compareTo(Cut.posInfinity()));
    }

    @Test
    void equalsAndHashCodeAgreeWithCompareTo() {
        assertEquals(Cut.below(5), Cut.below(5));
        assertEquals(Cut.below(5).hashCode(), Cut.below(5).hashCode());
        assertEquals(0, Cut.below(5).compareTo(Cut.below(5)));
        // BELOW(5) and ABOVE(5) are distinct cuts straddling value 5.
        assertTrue(!Cut.below(5).equals(Cut.above(5)));
    }
}
