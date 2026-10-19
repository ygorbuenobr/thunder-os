#!/usr/bin/env python3
"""Reject accidental model/copy-generation dependencies in the cadence runtime."""
import argparse
import ast
from pathlib import Path

FILES=(Path("_ferramentas/thunder_os/hermes_linkedin_cadence.py"),
       Path("_ferramentas/thunder_os/hermes_linkedin_runtime.py"))
FORBIDDEN={"openai","anthropic","litellm","langchain","prompt","completion","chatcompletion","generate_copy"}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--check",choices=["test_runtime_is_deterministic"],required=True);parser.parse_args()
    failures=[]
    for path in FILES:
        tree=ast.parse(path.read_text(),filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                names=[alias.name for alias in node.names]
                if isinstance(node,ast.ImportFrom) and node.module:names.append(node.module)
                for name in names:
                    if name.split(".")[0].lower() in FORBIDDEN:failures.append(f"{path}: forbidden import {name}")
            if isinstance(node,ast.Name) and node.id.lower() in FORBIDDEN:failures.append(f"{path}: forbidden name {node.id}")
    if failures:raise SystemExit("\n".join(failures))
    print("hermes_linkedin_runtime_deterministic: ok")

if __name__=="__main__":main()
