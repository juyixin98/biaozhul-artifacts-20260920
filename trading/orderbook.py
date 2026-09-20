"""
内存订单簿（每个交易对一个实例）。

价格优先、同价时间优先：
- 买盘用最小堆存负价格，堆顶是最优（最高）买价；
- 卖盘用最小堆，堆顶是最优（最低）卖价；
- 每个价格档用 deque 按 id 升序（id 由数据库分配，单调递增即时间先后）排列订单；
- 撤单/成交完结采用惰性删除：从档位弹出时跳过已不在簿中的订单 id
  （簿内对象为浅拷贝快照，真实状态以数据库行锁下读取为准）。

内存簿只是数据库的缓存视图，可以随时从 NEW / PARTIALLY_FILLED 订单重建，
重建结果按 (价格, id) 排序，保持原有价格-时间顺序。
"""
import heapq
from collections import defaultdict, deque
from decimal import Decimal


class OrderBook:
    def __init__(self, pair_id: int):
        self.pair_id = pair_id
        self._bids_heap: list[Decimal] = []   # 存 -price
        self._asks_heap: list[Decimal] = []   # 存 price
        self._bid_levels: dict = defaultdict(deque)   # price -> deque[order_id]
        self._ask_levels: dict = defaultdict(deque)
        # 在簿订单 id 集合（惰性删除的判据）
        self.resting: set[int] = set()

    # ---------- 重建 ----------
    def restore(self, open_orders) -> None:
        """open_orders: 已按 (side, price, id) 排好序的订单可迭代对象。"""
        self._bids_heap.clear()
        self._asks_heap.clear()
        self._bid_levels.clear()
        self._ask_levels.clear()
        self.resting.clear()
        bid_prices, ask_prices = set(), set()
        for o in open_orders:
            self.resting.add(o.id)
            if o.side == "BUY":
                levels, prices = self._bid_levels, bid_prices
                key = -o.limit_price
                heap = self._bids_heap
            else:
                levels, prices = self._ask_levels, ask_prices
                key = o.limit_price
                heap = self._asks_heap
            levels[o.limit_price].append(o.id)
            if key not in prices:
                prices.add(key)
                heapq.heappush(heap, key)

    # ---------- 挂单进入簿 ----------
    def add_resting(self, order) -> None:
        if order.side == "BUY":
            self._push_level(self._bids_heap, self._bid_levels, -order.limit_price,
                             order.limit_price, order.id)
        else:
            self._push_level(self._asks_heap, self._ask_levels, order.limit_price,
                             order.limit_price, order.id)
        self.resting.add(order.id)

    @staticmethod
    def _push_level(heap, levels, heap_key, price, order_id):
        if price not in levels or not levels[price]:
            heapq.heappush(heap, heap_key)
        levels[price].append(order_id)

    # ---------- 惰性清理堆顶 ----------
    def _clean_top(self, heap, levels, sign: int):
        """sign=1 表示堆里直接存价格(卖)；sign=-1 表示存负价格(买)。"""
        while heap:
            price = sign * heap[0]
            dq = levels.get(price)
            while dq and dq[0] not in self.resting:
                dq.popleft()
            if dq:
                return price, dq
            heapq.heappop(heap)
        return None, None

    def best_bid(self):
        return self._clean_top(self._bids_heap, self._bid_levels, -1)

    def best_ask(self):
        return self._clean_top(self._asks_heap, self._ask_levels, 1)

    def remove(self, order_id: int, side: str, price: Decimal) -> None:
        """惰性删除：标记不在簿中，堆顶访问时再弹出。重复调用安全。"""
        self.resting.discard(order_id)

    def snapshot(self) -> dict:
        """调试/观察用：聚合成档位视图。"""
        def agg(heap, levels, sign):
            out = []
            for key in sorted(heap):
                price = sign * key
                ids = [i for i in levels.get(price, ()) if i in self.resting]
                if ids:
                    out.append({"price": str(price), "order_ids": ids, "count": len(ids)})
            return out
        return {
            "pair_id": self.pair_id,
            "bids": agg(self._bids_heap, self._bid_levels, -1),
            "asks": agg(self._asks_heap, self._ask_levels, 1),
        }
