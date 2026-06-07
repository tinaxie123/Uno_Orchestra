"""Prompt templates for legacy environment managers.

The active Uno environment uses configs/uno/system_prompt.txt. These templates
keep the generic environment manager importable for optional legacy tasks.
"""

SEARCH_TEMPLATE_NO_HIS = "{task_description}"
SEARCH_TEMPLATE = "{task_description}\n\nHistory:\n{memory_context}\n\nStep: {step_count}"

ALFWORLD_TEMPLATE_NO_HIS = "{task_description}\n\nObservation:\n{observation}\n\nAdmissible commands:\n{admissible_commands}"
ALFWORLD_TEMPLATE = ALFWORLD_TEMPLATE_NO_HIS + "\n\nHistory:\n{memory_context}"

SOKOBAN_TEMPLATE_NO_HIS = "{observation}"
SOKOBAN_TEMPLATE = "{observation}\n\nHistory:\n{memory_context}"
SOKOBAN_VISUAL_TEMPLATE = SOKOBAN_TEMPLATE

GYM_CARDS_EZPOINTS_TEMPLATE = "{observation}"
GYM_CARDS_POINTS24_TEMPLATE = "{observation}"
GYM_CARDS_NUMBERLINE_TEMPLATE = "{observation}"
GYM_CARDS_BLACKJACK_TEMPLATE = "{observation}"

WEBSHOP_TEMPLATE_NO_HIS = "{observation}"
WEBSHOP_TEMPLATE = "{observation}\n\nHistory:\n{memory_context}"

APPWORLD_TEMPLATE_NO_HIS = "{observation}"
APPWORLD_TEMPLATE = "{observation}\n\nHistory:\n{memory_context}"
