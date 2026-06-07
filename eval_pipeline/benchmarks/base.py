"""
Abstract benchmark interface. All benchmarks implement: load, format_question, verify.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Task:
    """One evaluation instance."""
    task_id: str
    raw: dict              # Original benchmark data
    question: str = ""     # Formatted question for the router
    context: dict = None   # Extra context passed to router (repo, instruction, etc.)
    gold: str = ""         # Ground truth (if available)


@dataclass
class VerifyResult:
    """Verification result for one task."""
    task_id: str
    reward: float          # 0.0 or 1.0 (or float for partial credit)
    error: Optional[str] = None
    log: str = ""


class BaseBenchmark(ABC):
    scoring_mode: str = "official_compatible"
    score_name: str = "Official-compatible score"

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def load(self, max_tasks: int = None) -> List[Task]:
        """Load evaluation tasks."""
        ...

    @abstractmethod
    def extract_answer(self, router_output: str, task: Task) -> str:
        """Extract the relevant answer from router output (e.g., extract diff patch)."""
        ...

    @abstractmethod
    def verify(self, task: Task, answer: str, logs_dir: str = None) -> VerifyResult:
        """Verify answer correctness. May involve Docker execution."""
        ...

    def verify_batch(self, tasks: List[Task], answers: List[str],
                     logs_dir: str = None) -> List[VerifyResult]:
        """Batch verification (default: sequential). Override for batch APIs."""
        return [self.verify(t, a, logs_dir) for t, a in zip(tasks, answers)]
