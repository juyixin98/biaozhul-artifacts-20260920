package drvb.tests;

import java.io.File;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.stream.Stream;

/**
 * 零依赖测试运行器：扫描类路径下 drvb/tests 目录中的 *Test.class，
 * 反射执行全部带 {@link Test} 注解的 public static void 方法。
 *
 * <p>退出码 0 = 全部通过，1 = 存在失败。输出每一步结果与统计摘要。
 */
public final class TestRunner {

    public static void main(String[] args) throws Exception {
        Path classesRoot = args.length > 0
                ? Path.of(args[0])
                : Path.of("build/classes");

        List<Class<?>> testClasses = discoverTestClasses(classesRoot);
        if (testClasses.isEmpty()) {
            System.err.println("未在 " + classesRoot.toAbsolutePath() + " 下找到任何测试类");
            System.exit(1);
        }

        int total = 0;
        int passed = 0;
        final List<String> failures = Collections.synchronizedList(new ArrayList<>());

        for (Class<?> cls : testClasses) {
            // 实例方法测试：每类创建一个实例（字段在各测试内部自行 setUp 重置）
            Object instance = null;
            for (Method m : cls.getDeclaredMethods()) {
                if (!m.isAnnotationPresent(Test.class)) {
                    continue;
                }
                if (m.getParameterCount() != 0
                        || m.getReturnType() != void.class
                        || (!Modifier.isStatic(m.getModifiers())
                        && !Modifier.isPublic(m.getModifiers()))) {
                    System.out.println("  [SKIP] 非法签名（需 public void 无参）: "
                            + cls.getSimpleName() + "." + m.getName());
                    continue;
                }
                if (!Modifier.isStatic(m.getModifiers()) && instance == null) {
                    try {
                        var ctor = cls.getDeclaredConstructor();
                        ctor.setAccessible(true);
                        instance = ctor.newInstance();
                    } catch (NoSuchMethodException nsme) {
                        System.out.println("  [SKIP] 实例测试类缺少无参构造器: "
                                + cls.getSimpleName());
                        break;
                    }
                }
                total++;
                String label = cls.getSimpleName() + "." + m.getName();
                try {
                    m.setAccessible(true);
                    m.invoke(Modifier.isStatic(m.getModifiers()) ? null : instance);
                    passed++;
                    System.out.println("  [PASS] " + label);
                } catch (java.lang.reflect.InvocationTargetException ite) {
                    Throwable cause = ite.getCause();
                    failures.add(label + "  ->  "
                            + cause.getClass().getSimpleName() + ": " + cause.getMessage());
                    System.out.println("  [FAIL] " + label + "  ->  "
                            + cause.getClass().getSimpleName() + ": " + cause.getMessage());
                }
            }
        }

        System.out.println();
        System.out.println("================================================");
        System.out.println("测试摘要: " + passed + "/" + total + " 通过，"
                + (total - passed) + " 失败（扫描目录 " + classesRoot.toAbsolutePath() + "）");
        if (!failures.isEmpty()) {
            System.out.println("------------------------------------------------");
            for (String f : failures) {
                System.out.println("  ✗ " + f);
            }
        }
        System.out.println("================================================");
        System.exit(failures.isEmpty() ? 0 : 1);
    }

    private static List<Class<?>> discoverTestClasses(Path classesRoot) throws Exception {
        Path testDir = classesRoot.resolve("drvb/tests");
        if (!Files.isDirectory(testDir)) {
            return List.of();
        }
        List<String> names = new ArrayList<>();
        try (Stream<Path> walk = Files.walk(testDir)) {
            walk.filter(Files::isRegularFile)
                    .map(p -> classesRoot.relativize(p).toString())
                    .filter(s -> s.endsWith("Test.class"))
                    .filter(s -> !s.contains("$"))
                    .map(s -> s.substring(0, s.length() - ".class".length())
                            .replace(File.separatorChar, '.').replace('/', '.'))
                    .sorted()
                    .forEach(names::add);
        }
        URL rootUrl = classesRoot.toAbsolutePath().toUri().toURL();
        List<Class<?>> classes = new ArrayList<>();
        try (URLClassLoader cl = new URLClassLoader(new URL[]{rootUrl},
                TestRunner.class.getClassLoader())) {
            for (String name : names) {
                classes.add(Class.forName(name, true, cl));
            }
        }
        return classes;
    }
}
