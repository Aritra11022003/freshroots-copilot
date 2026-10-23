"""Local preview: serves index.html and /api/metrics (same code as on Vercel), keys from ../.env.

    .venv/bin/python dashboard/dev_server.py    # http://localhost:8600
"""
import importlib.util
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
spec = importlib.util.spec_from_file_location("metrics", HERE / "api" / "metrics.py")
metrics = importlib.util.module_from_spec(spec); spec.loader.exec_module(metrics)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            data = json.dumps(metrics.build_metrics(metrics.supabase_fetch), default=str).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(data)
        else:
            super().do_GET()


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8600), Handler).serve_forever()
