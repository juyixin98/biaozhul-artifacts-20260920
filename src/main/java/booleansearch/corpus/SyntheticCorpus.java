package booleansearch.corpus;

import booleansearch.index.InvertedIndex;
import booleansearch.model.Document;

import java.util.List;

/**
 * 自建合成语料：不读取外部数据，全部内容在代码内确定。
 *
 * <p>12 篇短文档，围绕受控词表编写，词频刻意设计（df = 出现文档数）：
 * <ul>
 *   <li>常见词：{@code pet}(7)、{@code cat}(6)、{@code dog}(5)、{@code food}(4)、
 *       {@code coffee}(4)、{@code tea}(4)</li>
 *   <li>中等：{@code city}(3)、{@code garden}(3)、
 *       {@code quantum}(2)、{@code robot}(2)、{@code water}(2)</li>
 *   <li>稀有（df=1）：{@code zebra}、{@code xylophone}、{@code kayak}、
 *       {@code falcon}、{@code glacier}、{@code neon}、{@code park}</li>
 * </ul>
 * 这样 AND 查询的倒排链大小差异明显，交集顺序优化前后的探测次数可观察到差距。
 */
public final class SyntheticCorpus {

    private SyntheticCorpus() {
    }

    public static List<Document> documents() {
        return List.of(
                new Document(1, "猫与咖啡",
                        "the cat sits by the window while coffee is served "
                                + "a quiet pet in a coffee shop"),
                new Document(2, "狗的公园日",
                        "the dog runs in the park and plays with a ball "
                                + "every dog loves the park food after play a friendly pet day"),
                new Document(3, "城市宠物指南",
                        "cat and dog care in the city pet clinic food supplies "
                                + "and city pet registration for every cat and dog"),
                new Document(4, "茶园",
                        "tea leaves grow on the hillside the tea garden is calm "
                                + "fresh tea every morning in the garden a cat watches"),
                new Document(5, "量子计算入门",
                        "quantum computing uses qubits and quantum gates "
                                + "a short quantum theory primer without pet stories"),
                new Document(6, "机器人与猫",
                        "the small robot watches the cat nap the pet ignores the robot "
                                + "robot sensors and cat paws"),
                new Document(7, "咖啡与茶的城市",
                        "coffee and tea shops across the city serve pet owners a cat and a dog "
                                + "city coffee culture meets tea gardens and pet cakes"),
                new Document(8, "花园里的狗",
                        "the dog digs in the garden and eats food nearby "
                                + "a garden dog needs food water and shade"),
                new Document(9, "木琴课",
                        "a beginner xylophone class meets after tea practice "
                                + "the xylophone teacher also loves coffee"),
                new Document(10, "量子机器人",
                        "a quantum robot prototype combines quantum sensors with "
                                + "robot motion planning and neon lasers"),
                new Document(11, "孤舟皮划艇",
                        "the kayak glides past a glacier while a falcon circles "
                                + "cold water and a zebra stripe painted on the kayak"),
                new Document(12, "社区宠物食物日",
                        "pet food donations for cat and dog shelters coffee and tea "
                                + "for volunteers pet food pet care and city garden news")
        );
    }

    /** 把语料装入索引。 */
    public static void loadInto(InvertedIndex index) {
        for (Document doc : documents()) {
            index.putDocument(doc);
        }
    }
}
