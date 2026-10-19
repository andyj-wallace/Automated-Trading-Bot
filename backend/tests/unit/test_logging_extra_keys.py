import ast
import logging
import pathlib

RESERVED = set(logging.LogRecord("n", 0, "p", 0, "m", None, None).__dict__) | {"message", "asctime"}


def test_no_reserved_keys_in_logging_extra():
    hits = []
    app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
    for path in app_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                        hits += [
                            f"{path}:{k.lineno} {k.value}"
                            for k in kw.value.keys
                            if isinstance(k, ast.Constant) and k.value in RESERVED
                        ]
    assert not hits, hits
