package ppd;

/** 列引用的身份键。限定名为 null 表示裸列名。 */
public record RefKey(String qualifier, String name) {

    public boolean matches(Column c) {
        if (!c.name().equals(name)) return false;
        if (qualifier == null) return true; // 裸名：schema 解析阶段再判歧义
        return qualifier.equals(c.qualifier());
    }

    @Override
    public String toString() {
        return qualifier == null ? name : qualifier + "." + name;
    }
}
