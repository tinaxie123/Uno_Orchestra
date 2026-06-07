"""
Projection function for UNO.
Maps raw LLM text output to actions the environment can process.
"""

import re
from typing import List, Tuple

from uno_orchestor.routing.uno.primitives import VALID_PRIMITIVES

FINAL_RE = re.compile(r'<final_answer>(.*?)</final_answer>', re.DOTALL)
ROUTE_RE = re.compile(
    r'<route round="(\d+)" subtask="(\d+)" model="([^"]+)" skill="([^"]+)">(.*?)</route>',
    re.DOTALL,
)
PLAN_RE = re.compile(r'<plan round="(\d+)">', re.DOTALL)


def uno_projection(text_actions: List[str]) -> Tuple[List[str], List[int]]:
    """
    Validate and pass through model outputs.

    Returns:
        actions: List[str] - the raw text actions (passed through)
        valids: List[int] - 1 if action contains valid schema tags, 0 otherwise
    """
    actions = []
    valids = []
    for text in text_actions:
        # An action is valid if it contains at least one of:
        # - <final_answer> (lazy mode or completion)
        # - <plan> + <route> (decomposition)
        # - <verify> (verification step)
        has_final = bool(FINAL_RE.search(text))
        routes = ROUTE_RE.findall(text)
        has_route = bool(routes)
        has_plan = bool(PLAN_RE.search(text))
        routes_use_valid_primitives = all(route[3] in VALID_PRIMITIVES for route in routes)

        is_valid = has_final or (has_plan and has_route and routes_use_valid_primitives)
        actions.append(text)
        valids.append(1 if is_valid else 0)

    return actions, valids
