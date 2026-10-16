"""Command line: python -m campfinder <command>"""
import argparse

from .config import Config


def main():
    ap = argparse.ArgumentParser(prog="campfinder", description="Minnesota campsite finder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", default="config.toml")
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                       help="override a config value for this run, e.g. criteria.lakes.max_distance_m=1000")

    p = sub.add_parser("check", help="list which configured input files exist")
    common(p)
    p = sub.add_parser("inspect", help="show columns and values of a data file, to write land queries")
    p.add_argument("path")
    p.add_argument("--layer")
    p = sub.add_parser("build", help="run the overlay and write the vector layers")
    common(p)
    p = sub.add_parser("serve", help="open the interactive map")
    common(p)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("run", help="build, then serve")
    common(p)
    p.add_argument("--port", type=int, default=8000)

    a = ap.parse_args()

    if a.cmd == "inspect":
        from .tools import inspect
        inspect(a.path, a.layer)
        return
    cfg = Config.load(a.config, a.set)
    if a.cmd == "check":
        from .tools import check
        check(cfg)
    elif a.cmd == "build":
        from .build import build
        build(cfg)
    elif a.cmd == "serve":
        from .tools import serve
        serve(cfg, a.port, not a.no_browser)
    elif a.cmd == "run":
        from .build import build
        from .tools import serve
        build(cfg)
        serve(cfg, a.port)


if __name__ == "__main__":
    main()
