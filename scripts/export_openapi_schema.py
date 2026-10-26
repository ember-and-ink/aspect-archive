"""Write FastAPI's OpenAPI document to `frontend/openapi.json`.

Step one of two in `make api-schema`; step two runs `openapi-typescript` over
the file this writes to produce `frontend/src/lib/api-schema.ts`, which is the
only description of the server the React client ever sees.

The JSON is an intermediate and is gitignored. The generated TypeScript is
committed, and CI re-runs the whole target and fails on a diff -- that check is
what keeps the client honest against the routes, and it is the reason every API
change in docs/UI_REWRITE_PLAN.md §3 is a schema change first and a UI change
second.
"""

from __future__ import annotations

import json
from pathlib import Path

from aspect.server import app

OUTPUT = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # A trailing newline so the file is a well-formed text file and a diff
    # against a regenerated copy is not permanently one line off.
    OUTPUT.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
