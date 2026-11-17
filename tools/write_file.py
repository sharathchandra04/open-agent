"""write_file — the only way the agent writes code for itself.

Safety rule, enforced here not by convention: it may only write
inside tools/. It can never touch agent.py or supervisor.py.
Core changes go through a diff the user approves, by hand.
"""
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

TOOL = {
    "name": "write_file",
    "description": "Write text to a file inside tools/ (new tool code only).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Filename inside tools/, e.g. 'add_numbers.py'"},
            "content": {"type": "string", "description": "File contents"},
        },
        "required": ["path", "content"],
    },
}


def run(path, content):
    target = os.path.abspath(os.path.join(TOOLS_DIR, path))
    if not target.startswith(TOOLS_DIR + os.sep):
        return f"refused: {path!r} is outside tools/"
    if os.path.basename(target) in ("agent.py", "supervisor.py"):
        return "refused: agent.py / supervisor.py are never written by the agent"
    with open(target, "w") as f:
        f.write(content)
    return f"wrote {target}"
