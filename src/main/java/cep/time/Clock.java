package cep.time;

/** 可注入的时钟：生产环境用系统时间，测试用固定/虚拟时间。 */
public interface Clock {
    long nowMillis();

    static Clock system() {
        return System::currentTimeMillis;
    }

    static Clock fixed(long t) {
        return () -> t;
    }
}
