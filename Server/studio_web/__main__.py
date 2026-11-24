"""``python -m studio_web serve [--host 0.0.0.0] [--port 8780]``"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="studio_web")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the browser gateway")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8780)
    s.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "serve":
        here = Path(__file__).resolve().parents[1]
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        import uvicorn

        from studio_web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, workers=1,
                    log_level=args.log_level, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
