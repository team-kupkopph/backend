"""Read the mobile app's TypeScript types so a backend test can compare them with what the API sends.

`mobile_app` is a SEPARATE repository. In a backend-only checkout (what CI clones) there is nothing to
read, so a test that uses this skips; it runs wherever `mobile_app/` sits beside `backend/` (the docs
workspace), or with KUPKOP_MOBILE_APP pointing at a checkout. The same parser as pets/tests and
shelter/tests carry privately; those two can move onto this one.
"""
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]    # the docs workspace, one level above backend/
MOBILE_SRC = pathlib.Path(os.environ.get("KUPKOP_MOBILE_APP", ROOT / "mobile_app")) / "src"


def available(*relative):
    return all((MOBILE_SRC / r).exists() for r in relative)


def ts_keys(name, *files):
    """The top-level keys of `export type <name> = { ... }` in the first of `files` (relative to
    mobile_app/src) that declares it, optional keys included. Several files are listed when a type has
    moved between them, so the contract holds on either side of the move."""
    for rel in files:
        source = (MOBILE_SRC / rel).read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        source = re.sub(r"//[^\n]*", "", source)
        match = re.search(rf"export type {name} = \{{", source)
        if not match:
            continue
        depth, body = 1, []
        for ch in source[match.end():]:
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                break
            body.append(ch if depth == 1 else " ")     # blank out nested objects' own keys
        keys = set(re.findall(r"(\w+)\??\s*:", "".join(body)))
        assert keys, f"parsed no keys from {name}"       # guards under-report: never compare against nothing
        return keys
    raise AssertionError(f"no `export type {name} = {{` in any of {files}")
