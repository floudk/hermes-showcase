"""make test 跑这个：三个断言分别对应零拷贝、边界检查、生命周期。"""
import time

import numpy as np

import factorlib as fl


def t1_correctness():
    x = np.arange(10, dtype=float)
    ma = fl.moving_average(x, 3)
    assert np.isnan(ma[:2]).all(), "前 window-1 个应该是 NaN"
    np.testing.assert_allclose(ma[2:], [1, 2, 3, 4, 5, 6, 7, 8])
    print("✔ 样例1 正确性：MA(3) 前两项 NaN，其余正确")


def t2_no_hidden_copy():
    """同一个 C++ 函数：.noconvert() 让它拒绝；不加就静默复制一份。"""
    a = np.arange(8_000_000, dtype=float)[::2]  # 1 维、步长 2 -> 非连续，32MB
    assert not a.flags["C_CONTIGUOUS"], "构造用例失败"

    t0 = time.perf_counter()
    try:
        fl.moving_average(a, 3)
    except TypeError as e:
        t1 = time.perf_counter()
        print(f"✔ 严格版（.noconvert()）拒绝非连续输入，耗时 {(t1-t0)*1e3:.3f} ms"
              f" | {str(e)[:40]}…")
    else:
        raise SystemExit("✘ 严格版竟然接受了非连续数组")

    t2 = time.perf_counter()
    r = fl.moving_average_lenient(a, 3)
    t3 = time.perf_counter()
    same = np.allclose(r, fl.moving_average(np.ascontiguousarray(a), 3), equal_nan=True)
    print(f"✔ 宽松版（默认）接受并静默复制 32MB：{(t3-t2)*1e3:.1f} ms，结果一致={same}"
          f"  <- 碎片时间的性能陷阱就在这")


def t2b_ndim_guard():
    try:
        fl.moving_average(np.zeros((3, 3)), 3)
    except ValueError as e:
        print(f"✔ 样例1 维度守卫生效：{e}")


def t3_zero_copy():
    fm = fl.FactorMatrix(4, 5)
    fm.fill(1.5)
    a = fm.view()
    assert a.base is fm, f"视图的 base 应该是 FactorMatrix 本身，实际 {a.base!r}"
    a[0, 0] = 42.0
    got = fm.sum()
    assert got == 42.0 + 1.5 * 19, got
    print(f"✔ 样例2 零拷贝：改 numpy 视图 -> C++ 侧 sum() 立刻变 {got}")


def t4_o1_view():
    fm = fl.FactorMatrix(2000, 2000)  # 4e6 个 double ≈ 32 MB
    t0 = time.perf_counter()
    _ = fm.view()
    t1 = time.perf_counter()
    t2 = time.perf_counter()
    _ = fm.view().copy()
    t3 = time.perf_counter()
    print(f"✔ 样例2 view: {(t1-t0)*1e3:.3f} ms   copy: {(t3-t2)*1e3:.2f} ms  (32MB)")
    assert (t1 - t0) * 1e3 < 1.0, "view 应该是 O(1)"


def t5_gil_release_scaling():
    """真正的信号是"加速比"：4 线程相对 1 线程跑同一份 CPU 活，快多少倍。"""
    import os
    import threading

    def wall(fn, k):
        ts = [threading.Thread(target=fn) for _ in range(k)]
        t0 = time.perf_counter()
        [t.start() for t in ts]
        [t.join() for t in ts]
        return time.perf_counter() - t0

    N = 100_000_000          # 要够重，否则线程启动开销盖过并行收益
    cpp1 = wall(lambda: fl.busy_sum(N), 1)
    cpp4 = wall(lambda: fl.busy_sum(N), 4)

    def pybusy():
        s = 0.0
        for i in range(300_000):
            s += i * 0.5

    py1 = wall(pybusy, 1)
    py4 = wall(pybusy, 4)

    # 正确的指标是吞吐：4 个任务串行需要 4×单任务时间，实测花了多少
    cpp_speed = (4 * cpp1) / cpp4
    py_speed = (4 * py1) / py4

    print(f"  CPU 核数 {os.cpu_count()}")
    print(f"✔ 样例3 C++(释放GIL)：4 个任务串行需 {4*cpp1*1e3:.0f} ms，4 线程实测 "
          f"{cpp4*1e3:.0f} ms -> 吞吐加速 {cpp_speed:.2f}x")
    print(f"✔ 样例3 纯Python(握GIL)：4 个任务串行需 {4*py1*1e3:.0f} ms，4 线程实测 "
          f"{py4*1e3:.0f} ms -> 吞吐加速 {py_speed:.2f}x")
    assert cpp_speed > 2.0, "释放 GIL 的 C++ 竟然没并行，检查 gil_scoped_release"
    assert py_speed < 1.5, "纯 Python 线程竟然并行了？环境不对"


if __name__ == "__main__":
    for f in (t1_correctness, t2_no_hidden_copy, t2b_ndim_guard, t3_zero_copy,
              t4_o1_view, t5_gil_release_scaling):
        f()
    print("\n全部通过。改 factorlib.cpp 里任意一处，再 make test 看变化。")
