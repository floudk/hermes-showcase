// factorlib.cpp — 10 分钟绑定练习场
// 三个可亲手改动的最小样例：零拷贝入参 / 零拷贝出参数组 / 生命周期 & GIL
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

// ---------- 样例 1：numpy 数组零拷贝进、新数组出，计算期间放掉 GIL ----------
py::array_t<double> moving_average(py::array_t<double, py::array::c_style> x, int window) {
    if (window <= 0) throw std::invalid_argument("window 必须 > 0");
    auto buf = x.request();                       // 只拿描述符 + 裸指针，不复制
    if (buf.ndim != 1) throw std::invalid_argument("moving_average 只接受 1 维数组");
    const py::ssize_t n = buf.shape[0];

    auto out = py::array_t<double>(n);            // 新分配（这个是要拷的）
    auto obuf = out.request();
    const double* in = static_cast<const double*>(buf.ptr);
    double* q = static_cast<double*>(obuf.ptr);

    {
        py::gil_scoped_release release;           // 纯 C++ 计算期间放掉 GIL
        double acc = 0.0;
        for (py::ssize_t i = 0; i < n; ++i) {
            acc += in[i];
            if (i >= window) acc -= in[i - window];
            q[i] = (i + 1 >= window) ? acc / window : std::nan("");
        }
    }                                             // 作用域结束恢复 GIL
    return out;                                   // 构造返回值要 GIL，必须放回
}

// ---------- 样例 3：放掉 GIL 才可能真并行 ----------
double busy_sum(long n) {
    py::gil_scoped_release release;
    double s = 0.0;
    for (long i = 1; i <= n; ++i) s += static_cast<double>(i) * 0.5;
    return s;
}

// ---------- 样例 2：C++ 持有内存，Python 侧是同一块内存的视图（O(1)） ----------
class FactorMatrix {
public:
    FactorMatrix(py::ssize_t rows, py::ssize_t cols)
        : rows_(rows), cols_(cols),
          data_(std::make_shared<std::vector<double>>(
              static_cast<std::size_t>(rows) * static_cast<std::size_t>(cols), 0.0)) {}

    py::ssize_t rows() const { return rows_; }
    py::ssize_t cols() const { return cols_; }
    void fill(double v) { std::fill(data_->begin(), data_->end(), v); }

    double sum() const {
        py::gil_scoped_release release;
        double s = 0.0;
        for (double v : *data_) s += v;
        return s;
    }

    // base 传 Python 对象本身：视图活着 => 这个对象不会被回收 => 指针不会悬垂
    py::array_t<double> view_array(py::handle base) {
        return py::array_t<double>(
            {rows_, cols_},
            {cols_ * static_cast<py::ssize_t>(sizeof(double)),
             static_cast<py::ssize_t>(sizeof(double))},
            data_->data(), base);
    }

private:
    py::ssize_t rows_, cols_;
    std::shared_ptr<std::vector<double>> data_;
};

PYBIND11_MODULE(factorlib, m) {
    m.doc() = "10 分钟绑定练习场：零拷贝、GIL、生命周期";
    m.attr("__version__") = "0.1.0";

    m.def("busy_sum", &busy_sum, py::arg("n"),
          "CPU 密集、期间释放 GIL：用来量线程到底有没有真并行");
    // 同一个 C++ 函数，两种绑定写法：差别全在 .noconvert()
    m.def("moving_average", &moving_average, py::arg("x").noconvert(), py::arg("window"),
          "严格版：非连续 / 非 double 数组直接 TypeError，杜绝隐藏拷贝");
    m.def("moving_average_lenient", &moving_average, py::arg("x"), py::arg("window"),
          "宽松版：需要时静默复制一份（这是默认行为，也是性能陷阱）");

    py::class_<FactorMatrix, std::shared_ptr<FactorMatrix>>(m, "FactorMatrix")
        .def(py::init<py::ssize_t, py::ssize_t>(), py::arg("rows"), py::arg("cols"))
        .def_property_readonly("rows", &FactorMatrix::rows)
        .def_property_readonly("cols", &FactorMatrix::cols)
        .def("fill", &FactorMatrix::fill, py::arg("v"))
        .def("sum", &FactorMatrix::sum)
        .def(
            "view",
            [](py::object self) { return self.cast<FactorMatrix&>().view_array(self); },
            "返回与 C++ 缓冲共享内存的 numpy 视图（O(1)，无拷贝）")
        .def("__repr__", [](const FactorMatrix& f) {
            return "<FactorMatrix " + std::to_string(f.rows()) + "x" +
                   std::to_string(f.cols()) + ">";
        });
}
