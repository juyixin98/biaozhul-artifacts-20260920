package hlc;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Marks a {@code public static void} method run once after all {@link Test} methods of a
 * class. Used to release resources (e.g. stop a network server whose internal dispatcher
 * thread is not a daemon).
 */
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.METHOD)
public @interface AfterAll {
}
