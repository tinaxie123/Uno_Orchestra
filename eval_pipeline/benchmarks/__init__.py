from .base import BaseBenchmark, Task, VerifyResult
from .swebench import SWEBench
from .terminalbench import TerminalBench
from .gpqa import GPQA
from .mmlu import MMLU
from .math_bench import MATH500
from .aime import AIME
from .drop_bench import DROP
from .humaneval_bench import HumanEval
from .mbpp_bench import MBPP
from .gaia import GAIA
from .livecodebench import LiveCodeBench
from .toolbench import ToolBench
from .mrcr import MRCR

BENCH_REGISTRY = {
    # Agentic (need environment interaction)
    "swebench": SWEBench,
    "terminalbench": TerminalBench,
    # Multiple-choice QA
    "gpqa": GPQA,
    "mmlu": MMLU,
    # Math
    "math500": MATH500,
    "aime": AIME,
    # Reading comprehension
    "drop": DROP,
    # Code generation
    "humaneval": HumanEval,
    "mbpp": MBPP,
    "livecodebench": LiveCodeBench,
    # Multi-tool reasoning
    "gaia": GAIA,
    # Tool routing
    "toolbench": ToolBench,
    # Long-context reasoning
    "mrcr": MRCR,
}
