"""Deterministic worker-only archive; never copies the management application."""
import ast
import io
import zipfile


MODULES = ("__init__", "config", "errors", "identity", "profile", "runtime", "storage")
TOOL_FUNCTIONS = ("refused", "bin_dir", "resolve", "child_path")
TOOL_CONSTANTS = ("SYSTEM_PATH", "NAMES", "FAILURE")
ENTRY = b'''"""Worker only: no operator management commands."""
import os
from pathlib import Path
import sys
from buzz_server.storage import Store, agent_id
from buzz_server.runtime import worker
if __name__ == "__main__":
    try:
        if os.getuid() == 0 or len(sys.argv) not in (3, 4):
            sys.exit(1)
        mode = sys.argv[3] if len(sys.argv) == 4 else None
        if mode not in (None, "local", "prompt-0", "prompt-1", "prompt-2"):
            sys.exit(1)
        tools = Path(sys.argv[0]).parent if mode else None
        sys.exit(worker(Store(sys.argv[1]), agent_id(sys.argv[2]), dedicated=True,
                        tools=tools, managed_prompt=mode not in (None, "local")))
    except Exception:
        sys.exit(1)
'''


def source(name):
    # Works from source and from an installed zipapp without opening an operator
    # installation to the target. Only the explicit allowlist is packaged.
    import importlib.util
    spec = importlib.util.find_spec("buzz_server." + name)
    value = spec.loader.get_source("buzz_server." + name)
    if value is None:
        raise ValueError("worker source unavailable")
    return value


def tools_source():
    original = source("dependencies")
    tree = ast.parse(original)
    segments = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in TOOL_FUNCTIONS:
            segments[node.name] = ast.get_source_segment(original, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in TOOL_CONSTANTS:
                    segments[target.id] = ast.get_source_segment(original, node)
    if set(segments) != set(TOOL_FUNCTIONS + TOOL_CONSTANTS):
        raise ValueError("worker tool boundary changed")
    # Dedicated workers use only the pinned tools staged in their private state.
    segments["resolve"] = '''def resolve(name, root=None):
    if name not in NAMES or root is None:
        refused()
    path = bin_dir(root) / name
    return str(path) if path.exists() else None'''
    segments["child_path"] = '''def child_path(root):
    return str(bin_dir(root)) + ':' + SYSTEM_PATH'''
    return ("import os\nfrom pathlib import Path\nimport shutil\nimport stat\n"
            "from .errors import Error\nfrom .storage import private_dir\n\n" +
            "\n\n".join(segments[n] for n in TOOL_CONSTANTS + TOOL_FUNCTIONS) + "\n").encode()


def build():
    files = {"buzz_server/" + name + ".py": source(name).encode() for name in MODULES}
    files["buzz_server/dependencies.py"] = tools_source()
    files["__main__.py"] = ENTRY
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return out.getvalue()
