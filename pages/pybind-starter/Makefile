# 一句话重建：make 一条命令搞定（这也是碎片时间能学下去的关键）
PY   := python3
CXX  := c++
EXT  := $(shell $(PY) -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
INCS := $(shell $(PY) -m pybind11 --includes)
TARGET := factorlib$(EXT)

CXXFLAGS := -O3 -Wall -shared -std=c++17 -fPIC -fvisibility=hidden $(INCS)

all: $(TARGET)

$(TARGET): factorlib.cpp
	$(CXX) $< -o $@ $(CXXFLAGS)

test: all
	$(PY) test_factorlib.py

clean:
	rm -f factorlib*.so

.PHONY: all test clean
