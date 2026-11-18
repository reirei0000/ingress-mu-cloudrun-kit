"""Container entrypoint and command-line calculator."""

import json
import os
import sys
from .download import ensure_data


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command not in ("serve", "calc", "download", "build-tables"):
        raise SystemExit(
            "Usage: python -m mu_calculator [serve|calc p1 p2 p3|download|build-tables]"
        )
    from .tables import max_level
    max_level()  # Validate before downloads or any preparation.
    folder = os.environ.get("GRUMP_DATA", "/data/grump")
    ensure_data(folder)
    os.environ.setdefault("GRUMP_DIR", os.path.join(folder, "bil"))
    if command == "download":
        return
    from .tables import ensure_tables

    table = (ensure_tables(os.environ["GRUMP_DIR"])
             if command != "serve" else None)
    if command == "build-tables":
        return
    if command == "calc":
        from . import Grump, MODEL
        from .coordinates import calculate

        grump = Grump()
        grump.density_table = table
        print(json.dumps(dict(calculate(grump, sys.argv[2:]), model=MODEL)))
    else:
        import uvicorn
        from copy import deepcopy

        log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
        log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"

        uvicorn.run(
            "mu_calculator.api:app", host="0.0.0.0", port=8000, access_log=False,
            proxy_headers=False, log_config=log_config
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error)) from None
