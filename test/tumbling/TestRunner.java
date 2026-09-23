package tumbling;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** 零依赖的极简测试框架：@Test 注解 + 静态断言 + 反射执行。 */
public final class TestRunner {

    @Target(ElementType.METHOD)
    @Retention(RetentionPolicy.RUNTIME)
    public @interface Test {}

    public static void assertTrue(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    public static void eq(long actual, long expected, String msg) {
        if (actual != expected) throw new AssertionError(msg + " — 期望 " + expected + "，实际 " + actual);
    }

    public static void eq(Object actual, Object expected, String msg) {
        boolean same = actual == null ? expected == null : actual.equals(expected);
        if (!same) throw new AssertionError(msg + " — 期望 " + expected + "，实际 " + actual);
    }

    /** 在排放日志中查找指定窗口的最新一条输出。 */
    public static Map<String, Object> lastEmission(WindowEngine engine, String partition, long windowStart) {
        Map<String, Object> found = null;
        for (Map<String, Object> e : engine.emissionsView()) {
            if (partition.equals(e.get("partition")) && ((Number) e.get("windowStart")).longValue() == windowStart) {
                found = e;
            }
        }
        return found;
    }

    public static long countEmissions(WindowEngine engine, String partition, long windowStart, String type) {
        long n = 0;
        for (Map<String, Object> e : engine.emissionsView()) {
            if (partition.equals(e.get("partition"))
                    && ((Number) e.get("windowStart")).longValue() == windowStart
                    && type.equals(e.get("type"))) n++;
        }
        return n;
    }

    public static void main(String[] args) throws Exception {
        List<Class<?>> classes = new ArrayList<>();
        if (args.length == 0) {
            classes.add(Class.forName("tumbling.EngineTest"));
            classes.add(Class.forName("tumbling.JsonTest"));
            classes.add(Class.forName("tumbling.HttpTest"));
        } else {
            for (String a : args) classes.add(Class.forName(a));
        }
        int passed = 0, failed = 0;
        List<String> failures = new ArrayList<>();
        for (Class<?> c : classes) {
            Object instance = c.getDeclaredConstructor().newInstance();
            for (Method m : c.getDeclaredMethods()) {
                if (!m.isAnnotationPresent(Test.class)) continue;
                String name = c.getSimpleName() + "." + m.getName();
                try {
                    m.setAccessible(true);
                    m.invoke(instance);
                    System.out.println("  ✓ " + name);
                    passed++;
                } catch (java.lang.reflect.InvocationTargetException ite) {
                    Throwable cause = ite.getCause();
                    System.out.println("  ✗ " + name + "  -> " + cause);
                    failures.add(name + "  -> " + cause);
                    failed++;
                }
            }
        }
        System.out.println();
        System.out.println("通过 " + passed + " 个，失败 " + failed + " 个。");
        if (failed > 0) {
            System.out.println("失败用例:");
            for (String f : failures) System.out.println("  - " + f);
            System.exit(1);
        }
    }
}
