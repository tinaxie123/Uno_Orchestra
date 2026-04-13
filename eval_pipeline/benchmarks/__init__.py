from .base import BaseBenchmark, Task, VerifyResult
from .swebench import SWEBench
from .terminalbench import TerminalBench

BENCH_REGISTRY = {
    "swebench": SWEBench,
    "terminalbench": TerminalBench,
}
