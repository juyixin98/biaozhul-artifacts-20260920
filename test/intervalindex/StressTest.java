package intervalindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/** 大规模压力测试（手动运行）：2 万次随机插入/删除，区间端点用更大的坐标域。 */
public class StressTest {

    public static void main(String[] args) {
        int ops = args.length >= 1 ? Integer.parseInt(args[0]) : 20_000;
        int bound = args.length >= 2 ? Integer.parseInt(args[1]) : 1_000_000;
        long seed = args.length >= 3 ? Long.parseLong(args[2]) : 42L;

        Random rnd = new Random(seed);
        IntervalStore store = new IntervalStore();
        List<int[]> ref = new ArrayList<>();
        long t0 = System.nanoTime();

        for (int op = 0; op < ops; op++) {
            int lo = rnd.nextInt(bound);
            int hi = lo + 1 + rnd.nextInt(bound / 4 + 1);
            boolean insert = op < ops / 2 || rnd.nextInt(10) < 7;
            if (insert) {
                store.insert(lo, hi);
                ref.add(new int[]{lo, hi});
            } else {
                long removed = store.delete(lo, hi, 1);
                if (removed == 1) {
                    for (int i = 0; i < ref.size(); i++) {
                        if (ref.get(i)[0] == lo && ref.get(i)[1] == hi) {
                            ref.remove(i);
                            break;
                        }
                    }
                }
            }
            if (op % 2000 == 0) {
                TreeInvariants.verifyBoth(store, ref.size());
            }
        }
        TreeInvariants.verifyBoth(store, ref.size());

        // 随机覆盖计数与交集查询对照
        for (int q = 0; q < 200; q++) {
            long t = rnd.nextInt(bound + bound / 4);
            long brute = 0;
            for (int[] iv : ref) {
                if (iv[0] <= t && t < iv[1]) {
                    brute++;
                }
            }
            if (store.coverage(t) != brute) {
                throw new AssertionError("coverage mismatch at t=" + t);
            }
        }
        for (int q = 0; q < 200; q++) {
            int lo = rnd.nextInt(bound);
            int hi = lo + 1 + rnd.nextInt(bound / 4 + 1);
            long brute = 0;
            for (int[] iv : ref) {
                if (iv[0] < hi && iv[1] > lo) {
                    brute++;
                }
            }
            long got = store.queryOverlap(lo, hi).size();
            if (got != brute) {
                throw new AssertionError("overlap mismatch [%d,%d): %d vs %d".formatted(lo, hi, got, brute));
            }
        }

        double ms = (System.nanoTime() - t0) / 1e6;
        System.out.printf("StressTest OK: %d ops, %d intervals live, %.0f ms total%n",
                ops, ref.size(), ms);
    }
}
