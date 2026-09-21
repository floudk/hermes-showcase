# pybind11 练习场（碎片时间版）

**为什么存在**：碎片时间学 C++ 扩展，最大的敌人不是难度，是编译/环境摩擦——一次 3 分钟的构建就能吃掉一个 10 分钟的碎片。这里把环境磨好了：`make test` 一条命令重建+自证，**实测 3.6 秒**。

## 一次性准备（约 5 分钟，只做一次）
```bash
pip install pybind11 numpy      # 公司网络不通就换镜像源
cd pybind-starter
make test
```
看到 6 行 `✔` 就说明环境通了，之后每一块学习都从这里开始。

## 日常循环（每块 10 分钟）
```bash
make test          # 1) 先确认基线绿
vim factorlib.cpp  # 2) 只改一个小地方（下面有现成的练手位）
make test          # 3) 几秒内看到结果
```

## 现成的练手位（对应技术轨的周次）
| 周 | 改哪里 | 想清楚什么 |
|----|--------|-----------|
| W2 D1 | `m.def(...)` 加一个新函数 | 导出链路最短长什么样 |
| W2 D3 | `FactorMatrix` 加一个方法 | 绑定类 vs 绑定函数差在哪 |
| W2 D4 | 把 `std::invalid_argument` 换成自定义异常 | C++ 异常怎么变成 Python 异常 |
| W3 D2 | 跑 `t2_no_hidden_copy` | 静默拷贝的代价（实测 32MB → **9.5 ms**） |
| W3 D3 | 改 `view_array` 的 strides | shape/strides 怎么描述内存布局 |
| W3 D4 | 删掉 `view()` 里的 `base` 参数 | 悬垂指针立刻显形（可能段错误） |
| W4 D1 | 删掉 `gil_scoped_release` | 并行度怎么没的 |
| W6 D5 | 加 `-fsanitize=address` 重新编译 | 内存错误在编译期就现形 |

## 三个样例分别在教什么
1. **`moving_average` —— 零拷贝入参**：`request()` 只拿裸指针不复制；`c_style` 约束非连续数组；GIL 的正确释放位置（**必须在构造返回值之前把 GIL 放回**，这是新手最常炸的地方）。
2. **`FactorMatrix.view()` —— C++ 持有内存、Python 拿 32MB 的 O(1) 视图**（实测 0.003 ms vs 拷贝 2.5 ms），`base=` 决定生命周期。这就是你现在手写 numpy 裸指针那套，换成 pybind11 长什么样。
3. **`busy_sum` + 多线程 —— 释放 GIL 才真并行**：4 个任务，C++ 吞吐加速 **3.87x**，纯 Python 握着 GIL 只有 **1.02x**。

## 我踩过的坑（已修好，但值得知道）
- **`py::array_t<T, c_style>` 默认会静默复制非连续输入**，只有加 `.noconvert()` 才报错。练习场把同一个 C++ 函数绑了两个名字（`moving_average` / `moving_average_lenient`）就是为了让这个差别肉眼可见。
- 输出文件名必须是 `factorlib$(EXT_SUFFIX).so`，模块名对不上就 `ImportError`。
- `py::gil_scoped_release` 一定要放在**花括号作用域**里，别让它活到 `return` 之后。
- 测并行要用**吞吐**（4 个任务串行时间 ÷ 实测时间），别用「1 线程 vs 4 线程」的倒数——后者会得出 0.97x 的假结论。
- `-fvisibility=hidden` 必加，否则符号污染别的扩展。
