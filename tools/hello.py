"""hello — the example tool. Shows the shape every tool follows."""

TOOL = {
    "name": "hello",
    "description": "Say hello to someone.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Who to greet"},
        },
        "required": ["name"],
    },
}


def run(name="world"):
    return f"Hello, {name}!"
