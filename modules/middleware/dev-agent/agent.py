#!/usr/bin/env python3
import argparse, json, os, subprocess, sys, textwrap, pathlib
from typing import Any, Dict, List

try:
    from openai import OpenAI
except Exception as e:
    print(f"Missing dependency openai: {e}. pip install -r dev-agent/requirements.txt", file=sys.stderr)
    sys.exit(2)

WORKSPACE = pathlib.Path(os.environ.get("AGENT_WORKSPACE", "/workspace")).resolve()
MAX_FILE_BYTES = int(os.environ.get("AGENT_MAX_FILE_BYTES", "200000"))
MAX_CMD_OUTPUT = int(os.environ.get("AGENT_MAX_CMD_OUTPUT", "20000"))

def _safe_path(p: str) -> pathlib.Path:
    path = (WORKSPACE / p).resolve() if not os.path.isabs(p) else pathlib.Path(p).resolve()
    if WORKSPACE != path and WORKSPACE not in path.parents:
        raise ValueError(f"Path outside workspace denied: {p}")
    return path

def tool_list_dir(path: str = ".", max_entries: int = 400) -> str:
    p = _safe_path(path)
    if not p.exists():
        return f"NOT_FOUND: {p}"
    if p.is_file():
        st = p.stat()
        return f"FILE {p} size={st.st_size}"
    entries = []
    for root, dirs, files in os.walk(p):
        # prune heavy dirs
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', '.mypy_cache', '.ruff_cache', 'node_modules', '.state', 'backups', 'pg_data'}]
        for name in sorted(dirs + files):
            full = pathlib.Path(root) / name
            rel = full.relative_to(WORKSPACE)
            entries.append(str(rel) + ("/" if full.is_dir() else ""))
            if len(entries) >= max_entries:
                return "\n".join(entries) + f"\n... truncated ({max_entries})"
    return "\n".join(entries) or "(empty)"

def tool_read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    p = _safe_path(path)
    if not p.is_file():
        return f"NOT_FOUND_OR_NOT_FILE: {p}"
    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        data = data[:MAX_FILE_BYTES]
        note = f"\n[truncated to {MAX_FILE_BYTES} bytes]"
    else:
        note = ""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(offset - 1, 0)
    chunk = lines[start:start + max(limit, 0)]
    out = [f"{i+start+1}: {line[:2000]}" for i, line in enumerate(chunk)]
    return "\n".join(out) + note

def tool_write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"WROTE {p} bytes={len(content.encode('utf-8'))}"

def tool_apply_patch(path: str, old: str, new: str) -> str:
    p = _safe_path(path)
    if not p.is_file():
        return f"NOT_FOUND: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        return "OLD_NOT_FOUND"
    if text.count(old) != 1:
        return f"OLD_NOT_UNIQUE count={text.count(old)}"
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"PATCHED {p}"

def tool_run_cmd(command: str, timeout: int = 180) -> str:
    # run inside workspace via bash
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(WORKSPACE),
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "PWD": str(WORKSPACE)},
        )
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if len(out) > MAX_CMD_OUTPUT:
            out = out[:MAX_CMD_OUTPUT] + "\n[truncated]"
        return f"exit={proc.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s"

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List files/dirs under workspace path.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}, "max_entries": {"type": "integer", "default": 400}}, "required": []}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text file with 1-indexed offset/limit.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 400}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create/overwrite a file with content.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "apply_patch", "description": "Replace a unique old string with new in file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "run_cmd", "description": "Run a bash command in the workspace.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 180}}, "required": ["command"]}}},
]

def dispatch(name: str, args: Dict[str, Any]) -> str:
    try:
        if name == "list_dir": return tool_list_dir(**args)
        if name == "read_file": return tool_read_file(**args)
        if name == "write_file": return tool_write_file(**args)
        if name == "apply_patch": return tool_apply_patch(**args)
        if name == "run_cmd": return tool_run_cmd(**args)
        return f"UNKNOWN_TOOL {name}"
    except Exception as e:
        return f"TOOL_ERROR {name}: {e}"

def main():
    ap = argparse.ArgumentParser(description="Autonomous coding agent for Omni-Core middleware (dev only)")
    ap.add_argument("goal", nargs="?", help="Development goal/task")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL") or os.environ.get("OMNI_MODEL_ADMIN") or "gpt-4o-mini")
    ap.add_argument("--base-url", default=os.environ.get("AGENT_BASE_URL") or os.environ.get("OMNIROUTE_URL") or os.environ.get("OPENAI_BASE_URL"))
    ap.add_argument("--api-key", default=os.environ.get("AGENT_API_KEY") or os.environ.get("OMNIROUTE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "local")
    ap.add_argument("--max-steps", type=int, default=int(os.environ.get("AGENT_MAX_STEPS", "30")))
    ap.add_argument("--system", default=str(pathlib.Path(__file__).with_name("SYSTEM_PROMPT.md")))
    args = ap.parse_args()
    goal = args.goal or os.environ.get("AGENT_GOAL")
    if not goal:
        print("ERROR: no goal provided. Usage: agent.py \"<development task>\"", file=sys.stderr)
        sys.exit(1)
    sys_prompt = pathlib.Path(args.system).read_text(encoding="utf-8") if pathlib.Path(args.system).exists() else "You are a coding agent for this repo."
    client = OpenAI(base_url=args.base_url, api_key=args.api_key) if args.base_url else OpenAI(api_key=args.api_key)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": sys_prompt + f"\n\nWorkspace: {WORKSPACE}"},
        {"role": "user", "content": f"Task: {goal}\n\nWork autonomously until the task is solved or you are blocked. Use tools. Stay within scope."},
    ]
    for step in range(1, args.max_steps + 1):
        try:
            resp = client.chat.completions.create(model=args.model, messages=messages, tools=TOOLS, tool_choice="auto", temperature=0.2)
        except Exception as e:
            # fallback: no-tools single shot
            print(f"MODEL_ERROR: {e}", file=sys.stderr)
            try:
                resp = client.chat.completions.create(model=args.model, messages=messages, temperature=0.2)
                msg = resp.choices[0].message
                print(msg.content or "")
                return
            except Exception as e2:
                print(f"FATAL: {e2}", file=sys.stderr); sys.exit(3)
        msg = resp.choices[0].message
        # normalize assistant message
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ]
            messages.append(assistant_msg)
            for tc in tool_calls:
                name = tc.function.name
                try:
                    targs = json.loads(tc.function.arguments or "{}")
                except Exception:
                    targs = {}
                result = dispatch(name, targs)
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": result})
            continue
        # no tool calls -> final
        messages.append(assistant_msg)
        print(msg.content or "")
        return
    print("Reached max steps without final answer. Last context retained in run.", file=sys.stderr)
    sys.exit(4)

if __name__ == "__main__":
    main()
