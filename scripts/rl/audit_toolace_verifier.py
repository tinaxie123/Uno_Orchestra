#!/usr/bin/env python
"""Audit the ToolACE verifier against the training parquet.

Samples N rows where `extra_info['source'] == 'toolace'`, runs
`verify_toolace` in both strict and lenient modes, and emits a CSV you
can eyeball before trusting the reward signal. The goal is catching
systematic verifier bias — e.g. a gold format we forgot to parse, or
refusal patterns matching everything — BEFORE the bias gets
amplified by GRPO.

Usage:
    python scripts/rl/audit_toolace_verifier.py --n 50
    python scripts/rl/audit_toolace_verifier.py --n 200 --out toolace_audit.csv

Reads:   data/rl/train.parquet (unless --parquet given)
Writes:  toolace_audit.csv      (unless --out given)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter

# Put repo root + verifier dir on the path so the script runs from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_VERIF = os.path.join(_REPO, 'scripts', 'data', 'verifiers')
for p in (_REPO, _VERIF):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd

from toolace_call_verifier import (  # type: ignore
    verify_toolace,
    _parse_func_calls,
)


def _classify_gold(g: str) -> str:
    s = str(g).strip()
    if s.startswith('[') and '-[' in s and ';' in s:
        return 'bracket_dash'
    if s.startswith('<') and '|' in s:
        return 'angle_pipe'
    if s.startswith('[{') or s.startswith('{'):
        return 'json'
    if '(' in s and ')' in s:
        return 'pycall'
    return 'plain_text'


def _row_iter(df: pd.DataFrame):
    for _, row in df.iterrows():
        ei = row['extra_info']
        if isinstance(ei, str):
            try:
                ei = json.loads(ei)
            except Exception:
                continue
        if not isinstance(ei, dict):
            continue
        if (ei.get('source') or '').lower() != 'toolace':
            continue
        rm = row['reward_model']
        if isinstance(rm, str):
            try:
                rm = json.loads(rm)
            except Exception:
                continue
        gold = rm.get('ground_truth', '') if isinstance(rm, dict) else ''
        yield ei, gold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--parquet', default='data/rl/train.parquet')
    ap.add_argument('--n', type=int, default=50)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--out', default='toolace_audit.csv')
    args = ap.parse_args()

    df = pd.read_parquet(args.parquet)
    rows = list(_row_iter(df))
    print(f'[audit] parquet={args.parquet}  total_toolace={len(rows)}')
    if not rows:
        print('[audit] no toolace rows — nothing to do.')
        return

    random.seed(args.seed)
    sample = random.sample(rows, min(args.n, len(rows)))

    format_hist: Counter = Counter()
    lenient_hist: Counter = Counter()
    strict_hist: Counter = Counter()

    with open(args.out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['idx', 'gold_format', 'parsed_calls', 'strict', 'lenient',
                    'question_preview', 'gold_preview', 'pred_echo_score'])
        for i, (ei, gold) in enumerate(sample):
            fmt = _classify_gold(gold)
            format_hist[fmt] += 1
            parsed = _parse_func_calls(gold) or []

            # Baseline upper bound: feed gold back in as the prediction.
            # If this is < 1.0 the parser/matcher cannot even recover
            # gold-against-itself — a signal to look at the row.
            echo = verify_toolace(gold, gold, strict=False)

            # Proxy for "what a randomised policy might score":
            # use the question text itself as the prediction.
            q = ei.get('question') or ''
            len_r = verify_toolace(q, gold, strict=False)
            str_r = verify_toolace(q, gold, strict=True)

            lenient_hist[_bucket(len_r)] += 1
            strict_hist[_bucket(str_r)] += 1

            w.writerow([
                i, fmt, len(parsed),
                f'{str_r:.2f}', f'{len_r:.2f}',
                q[:120].replace('\n', ' '),
                str(gold)[:120].replace('\n', ' '),
                f'{echo:.2f}',
            ])

    print(f'[audit] wrote {args.out}')
    print('[audit] gold format distribution in sample:')
    for k, v in format_hist.most_common():
        print(f'        {k:15s}  {v}')
    print('[audit] strict score (question → gold, should be ~all 0):')
    for k, v in strict_hist.most_common():
        print(f'        {k:15s}  {v}')
    print('[audit] lenient score (question → gold):')
    for k, v in lenient_hist.most_common():
        print(f'        {k:15s}  {v}')

    # Echo-recoverability: gold against itself should always be 1.0.
    # If it isn't, the parser has a bug for that gold format.
    import subprocess
    cnt_low_echo = 0
    with open(args.out, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            if float(r['pred_echo_score']) < 1.0:
                cnt_low_echo += 1
    print(f'[audit] gold-vs-gold echo failures (parser gap): {cnt_low_echo} / {len(sample)}')
    if cnt_low_echo > 0:
        print('        -> open the CSV, filter pred_echo_score < 1.0, and fix the parser.')

    # Adversarial suite — this is what tells you the verifier isn't flat.
    run_adversary_suite(rows, k=min(args.n * 4, len(rows)), seed=args.seed)


def _bucket(x: float) -> str:
    if x <= 0.0:
        return '0.0'
    if x >= 1.0:
        return '1.0'
    return 'partial'


def run_adversary_suite(rows, k: int = 200, seed: int = 0) -> None:
    """Hammer the verifier without involving a policy model.

    Three cheap stress tests, each one guards against a different failure
    mode the 10-step RL run would otherwise have to discover the hard way:

      1) self-match         → upper bound, must be ~all 1.0
      2) cross-pair random  → lower bound, must be ~all 0.0
      3) near-miss mutation → must *drop* relative to self-match; this is
                              what catches "verifier always returns 1.0"

    This is a pure-CPU job: ~1 second per 200 samples.
    """
    import copy
    random.seed(seed)
    pool = random.sample(rows, min(k, len(rows)))

    # 1) self-match
    self_scores = [verify_toolace(g, g, strict=False) for _, g in pool]

    # 2) cross-pair (shuffle golds, score against wrong gold as pred)
    idx = list(range(len(pool)))
    random.shuffle(idx)
    # Ensure no fixed points (no self-pairing by accident)
    for i, j in enumerate(idx):
        if i == j:
            idx[i] = (j + 1) % len(pool)
    cross_scores = [
        verify_toolace(pool[idx[i]][1], pool[i][1], strict=False)
        for i in range(len(pool))
    ]

    # 3) near-miss: mutate gold to a plausibly-wrong prediction.
    #    - call gold: break the first fn name (exercises AST match)
    #    - text gold: replace with an affirmative, non-refusal reply
    #      (merely appending to the gold keeps `g in p` true — that
    #      passes substring but is a pathologically weak adversary).
    AFFIRMATIVE_NEAR_MISS = (
        "Sure, here is the answer: the value is 42 and the city is Paris."
    )
    near_scores = []
    for _, gold in pool:
        calls = _parse_func_calls(gold)
        if calls:
            m = copy.deepcopy(calls)
            m[0]['name'] = (m[0].get('name') or 'f') + 'X'
            pred = json.dumps(m)
        else:
            pred = AFFIRMATIVE_NEAR_MISS
        near_scores.append(verify_toolace(pred, gold, strict=False))

    def _hist(name, xs):
        b = Counter(_bucket(x) for x in xs)
        mean = sum(xs) / len(xs) if xs else 0.0
        print(f'[adv] {name:18s} n={len(xs)}  mean={mean:.3f}  '
              f'0.0={b.get("0.0",0)}  partial={b.get("partial",0)}  1.0={b.get("1.0",0)}')

    print('[adv] --- adversarial distribution check ---')
    _hist('self-match (↑)', self_scores)
    _hist('cross-pair (↓)', cross_scores)
    _hist('near-miss  (↓)', near_scores)

    # Spread is the actionable metric: self - cross must be ≥ 0.5 for
    # GRPO to see meaningful group-relative advantages.
    spread = (sum(self_scores) - sum(cross_scores)) / max(len(pool), 1)
    print(f'[adv] self-vs-cross spread: {spread:+.3f}  (want >= 0.5)')
    drop = (sum(self_scores) - sum(near_scores)) / max(len(pool), 1)
    print(f'[adv] self-vs-near  spread: {drop:+.3f}  (want >= 0.2)')


if __name__ == '__main__':
    main()
