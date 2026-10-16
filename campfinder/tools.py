"""inspect, check and serve commands."""
from __future__ import annotations

import functools
import http.server
import socketserver
import webbrowser
from pathlib import Path


def inspect(path, layer=None, max_rows=200_000):
    """Print layers, columns and the common values in each text column."""
    import pandas as pd
    import pyogrio

    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Not found: {path}")
    layers = pyogrio.list_layers(str(path))
    print(f"{path}\n  {len(layers)} layer(s)")
    for name, gtype in layers:
        info = pyogrio.read_info(str(path), layer=name)
        print(f"\n  [{name}] {gtype}  features={info['features']}  crs={info['crs']}")
        print(f"    fields: {', '.join(info['fields'])}")
    if path.suffix.lower() in (".pbf", ".osm"):
        print("\n(OSM file: attribute values not scanned. Roads use the 'highway' key.)")
        return
    target = layer or layers[0][0]
    print(f"\nValues in layer [{target}] (first {max_rows:,} rows; text columns with <= 60 distinct values):")
    df = pyogrio.read_dataframe(str(path), layer=target, read_geometry=False, max_features=max_rows)
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            n = df[col].nunique()
            if 0 < n <= 60:
                counts = df[col].value_counts(dropna=False)
                print(f"\n  {col} ({n} values)")
                for v, c in counts.items():
                    print(f"    {c:>8,}  {v}")
            elif n > 60:
                print(f"\n  {col}: {n:,} distinct values, e.g. {', '.join(map(str, df[col].dropna().unique()[:5]))}")


def check(cfg):
    """Report which configured inputs exist."""
    rows = []
    for key in ("campable", "exclude", "context"):
        for s in cfg.get(f"land.{key}", []):
            rows.append((f"land.{key}: {s['name']}", cfg.resolve(s["path"])))
    for key in ("sky", "lakes", "roads"):
        rows.append((f"inputs.{key}", cfg.resolve(cfg.get(f"inputs.{key}.path", ""))))
    sb = cfg.get("project.state_boundary", "")
    if sb:
        rows.append(("project.state_boundary", cfg.resolve(sb)))
    ok = True
    for label, p in rows:
        exists = p.exists()
        size = f"{p.stat().st_size / 1e6:,.1f} MB" if exists and p.is_file() else ""
        print(f"  [{'ok' if exists else '--'}] {label:45s} {p} {size}")
        ok &= exists
    print("\nAll inputs present." if ok else "\nMissing inputs are listed with [--]. See README.md for where to get them.")


def serve(cfg, port=8000, open_browser=True):
    web = Path(__file__).resolve().parent.parent / "web"
    data = cfg.resolve(cfg.get("outputs.dir", "output"))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            p = path.split("?", 1)[0].split("#", 1)[0]
            if p.startswith("/data/"):
                return str((data / p[len("/data/"):]).resolve())
            return super().translate_path(path)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *a):
            pass

    handler = functools.partial(Handler, directory=str(web))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"Serving {web}\nData from {data}\nOpen {url}   (Ctrl+C to stop)")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
