import argparse
import logging
import sys

from .app import VIEWS, run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lark", description="Front-end and service layer for OBS")
    ap.add_argument("--view", choices=VIEWS, default=None, help="page to open on start, defaults to the last one you used")
    ap.add_argument("--size", metavar="WxH", help="window size, for screenshots")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    size = None
    if a.size:
        try:
            w, h = a.size.lower().split("x")
            size = (int(w), int(h))
        except ValueError:
            ap.error("--size must look like 1600x1000")

    def hook(t, v, tb):
        logging.getLogger("lark").critical("unhandled exception", exc_info=(t, v, tb))
    sys.excepthook = hook
    return run(a.view, size)


if __name__ == "__main__":
    sys.exit(main())
