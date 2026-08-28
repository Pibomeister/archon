#!/usr/bin/env python3
"""Premise blind-verification prep: reduce premises.json to questions only, so the
premise-verify node answers from code without seeing the planner's answers.
A missing or empty premises.json yields an empty questions file (the no-premises
short-circuit).
Usage: strip-premise-answers.py <premises.json> <premises-questions.json>"""
import json
import os
import sys

src, dest = sys.argv[1], sys.argv[2]
premises = []
if os.path.isfile(src):
    premises = json.load(open(src, encoding="utf-8"))
questions = [{"id": p["id"], "question": p["question"]} for p in premises]
json.dump(questions, open(dest, "w", encoding="utf-8"), indent=2)
print(f"QUESTIONS={len(questions)}")
