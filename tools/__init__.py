"""Tool registry: discovers one file per tool in this folder.

Each tool module must define:
  TOOL = {"name": ..., "description": ..., "parameters": {...}}  # Grok schema
  def run(**kwargs): ...                                        # the work

The agent never imports a new tool in its own process until it has
passed the test step (see agent.test_new_tool). Hot-loading is just
importlib.import_module() + adding it to this registry.
"""
import importlib
import pkgutil

import tools as _pkg


def discover_tools():
    """Return {name: (schema, run_fn)} for every tool module found."""
    found = {}
    for mod in pkgutil.iter_modules(_pkg.__path__):
        module = importlib.import_module(f"tools.{mod.name}")
        schema = getattr(module, "TOOL", None)
        run = getattr(module, "run", None)
        if schema and run:
            found[schema["name"]] = (schema, run)
    return found


def hot_load(name):
    """Import (or re-import) tools.<name> after it passed testing."""
    module = importlib.import_module(f"tools.{name}")
    importlib.reload(module)
    return module.TOOL, module.run
