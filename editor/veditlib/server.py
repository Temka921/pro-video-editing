"""The visual editor: a small local web server plus one HTML page.

It edits the same project.json the CLI edits, through the same ops, so a person can
steer in the browser while an AI keeps working from the command line. The page polls
the project revision and redraws when anyone changes it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import subprocess

from . import grade, keys, masks, ops, presets, project as P, render
from .project import EditError, SCRIPTS

UI = Path(__file__).with_name("ui") / "index.html"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_UPLOAD = 20 * 1024 ** 3


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser closing a video connection mid-stream is normal, not worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.job = {"state": "idle"}
        self.job_lock = threading.Lock()
        self._catalog = (None, None)

    def start_render(self, preview: bool) -> dict:
        with self.job_lock:
            if self.job.get("state") == "running":
                raise EditError("a render is already running")
            self.job = {"state": "running", "preview": preview, "progress": 0.0}
        threading.Thread(target=self._render, args=(preview,), daemon=True).start()
        return self.job

    def _render(self, preview: bool) -> None:
        try:
            proj = P.load(self.path)

            def prog(f):
                self.job["progress"] = round(f, 3)

            out = render.render(proj, None, preview, prog)
            self.job = {"state": "done", "preview": preview, "file": out.name, "rev": proj.data["rev"],
                        "mtime": int(out.stat().st_mtime)}
        except EditError as exc:
            self.job = {"state": "error", "preview": preview, "message": str(exc)}
        except Exception as exc:  # never leave the UI waiting on a crashed thread
            self.job = {"state": "error", "preview": preview, "message": f"{type(exc).__name__}: {exc}"}

    def sfx(self, query: str, limit: int = 12) -> dict:
        sys.path.insert(0, str(SCRIPTS))
        import sfx_search
        import sfxlib as L

        env = os.environ.get("SFX_CATALOG")
        if not any(c and Path(c).expanduser().is_file() for c in (env, L.USER_DIR / "catalog.json", L.SKILL_ROOT / "catalog" / "catalog.json")):
            return {"available": False, "hint": "No sound library yet. Run: python scripts/setup_library.py"}
        cat = L.load_catalog()
        tokens = L.tokens(query)
        ranked = []
        for s in cat["sounds"]:
            sc = sfx_search.score_sound(s, tokens) if tokens else 1.0
            if sc > 0:
                ranked.append((-sc, s["name"], s))
        ranked.sort(key=lambda r: r[:2])
        keys = ("id", "name", "category", "dur", "peak_s")
        return {"available": True, "total": len(cat["sounds"]),
                "results": [{k: s[k] for k in keys} for _, _, s in ranked[:limit]]}


def make_handler(state: State, port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vedit"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # -- helpers ----------------------------------------------------------
        def reply(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None,
                  cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code: int = 200):
            self.reply(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def fail(self, msg: str, code: int = 400):
            self.json({"error": msg}, code)

        def local_only(self) -> bool:
            host = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
            if host not in LOCAL_HOSTS:
                self.fail("this editor only answers on localhost", 403)
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                self.fail("cross-site request refused", 403)
                return False
            return True

        def body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def renders(self, proj) -> dict:
            found = {}
            for name in ("preview", "final"):
                f = proj.out_dir / f"{name}.mp4"
                if f.is_file():
                    try:
                        rev = json.loads((proj.out_dir / f".{name}.json").read_text(encoding="utf-8")).get("rev")
                    except (OSError, json.JSONDecodeError):
                        rev = None
                    found[name] = {"file": f.name, "mtime": int(f.stat().st_mtime), "rev": rev}
            return found

        def media_preview(self, kind: str, asset_id: str):
            """A cached waveform PNG or thumbnail JPG for an asset, for the timeline."""
            if not re.fullmatch(r"a\d+", asset_id):
                return self.fail("bad asset id", 400)
            proj = P.load(state.path)
            a = proj.assets.get(asset_id)
            src = proj.asset_path(asset_id) if a else None
            if not a or not src.is_file():
                return self.fail("no such asset", 404)
            if kind == "wave" and not (a["kind"] == "audio" or a.get("has_audio")):
                return self.fail("no sound in this asset", 404)
            st = src.stat()
            cache = proj.dir / ".cache"
            cache.mkdir(exist_ok=True)
            tag = f"{asset_id}_{int(st.st_mtime)}_{st.st_size}"
            if kind == "wave":
                dest, mime = cache / f"wave_{tag}.png", "image/png"
                width = int(min(4000, max(200, a["duration"] * 40)))
                args = ["-i", str(src), "-filter_complex",
                        f"[0:a:0]aformat=channel_layouts=mono,showwavespic=s={width}x64:colors=white,"
                        "colorkey=color=black:similarity=0.12[o]", "-map", "[o]", "-frames:v", "1"]
            else:
                dest, mime = cache / f"thumb_{tag}.jpg", "image/jpeg"
                at = 0.0 if a["kind"] == "image" else min(1.0, a["duration"] * 0.3)
                args = ["-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=-2:48", "-q:v", "4"]
            if not dest.exists():
                tmp = dest.with_name(dest.stem + ".tmp" + dest.suffix)
                try:
                    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args, str(tmp)],
                                       capture_output=True, timeout=90)
                except subprocess.TimeoutExpired:
                    return self.fail("preview took too long", 504)
                if r.returncode or not tmp.is_file():
                    return self.fail("could not make a preview", 404)
                os.replace(tmp, dest)
            self.reply(200, dest.read_bytes(), mime, cache="private, max-age=3600")

        def current(self) -> dict:
            proj = P.load(state.path)
            return {
                "renders": self.renders(proj),
                "meta": {
                    "looks": grade.LOOKS, "color_fields": {k: list(v) for k, v in grade.FIELDS.items()},
                    "keyprops": {k: list(v) for k, v in keys.PROPS.items()}, "eases": list(keys.EASES),
                    "transitions": list(ops.TRANSITIONS), "canvas_presets": ops.PRESETS,
                    "mask_ranges": {k: list(v) for k, v in keys.MASK_RANGES.items()},
                    "mask_shapes": list(masks.SHAPES), "mask_effects": list(masks.EFFECTS),
                },
                "project": ops.describe(proj),
                "problems": ops.check(proj),
                "presets": sorted(v["name"] for v in presets.load_all().values()),
                "job": state.job,
                "name": proj.dir.name,
            }

        # -- GET -------------------------------------------------------------
        def do_GET(self):
            if not self.local_only():
                return
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    self.reply(200, UI.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    since = parse_qs(url.query).get("since", [""])[0]
                    proj = P.load(state.path)
                    if since == str(proj.data.get("rev")) and state.job.get("state") != "running":
                        self.json({"unchanged": True, "rev": proj.data.get("rev"), "job": state.job})
                    else:
                        self.json(self.current())
                elif url.path == "/api/sfx":
                    q = parse_qs(url.query).get("q", [""])[0]
                    self.json(state.sfx(q))
                elif url.path in ("/api/wave", "/api/thumb"):
                    self.media_preview(url.path[5:], parse_qs(url.query).get("asset", [""])[0])
                elif url.path.startswith("/out/"):
                    self.serve_out(unquote(url.path[5:]))
                else:
                    self.fail("not found", 404)
            except EditError as exc:
                self.fail(str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def serve_out(self, name: str):
            proj = P.load(state.path)
            f = (proj.out_dir / name)
            if "/" in name or "\\" in name or name.startswith(".") or not f.is_file():
                return self.fail("not found", 404)
            size = f.stat().st_size
            ctype = {"mp4": "video/mp4", "png": "image/png", "wav": "audio/wav"}.get(f.suffix[1:].lower(), "application/octet-stream")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            lo, hi, code = 0, size - 1, 200
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    lo = int(m.group(1))
                    hi = int(m.group(2)) if m.group(2) else size - 1
                else:
                    lo = max(0, size - int(m.group(2)))
                hi = min(hi, size - 1)
                if lo > hi:
                    return self.reply(416, b"", extra={"Content-Range": f"bytes */{size}"})
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(hi - lo + 1))
            self.send_header("Cache-Control", "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(lo)
                left = hi - lo + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # -- POST ------------------------------------------------------------
        def do_POST(self):
            if not self.local_only():
                return
            if self.headers.get("X-Vedit") != "1":
                return self.fail("missing X-Vedit header", 403)
            url = urlparse(self.path)
            try:
                if url.path == "/api/upload":
                    return self.upload(parse_qs(url.query).get("name", ["upload"])[0])
                try:
                    data = json.loads(self.body() or b"{}")
                except json.JSONDecodeError:
                    return self.fail("body is not valid JSON")
                if url.path == "/api/op":
                    steps = data.get("steps")
                    results, proj = ops.apply(state.path, steps)
                    self.json({"results": results, "rev": proj.data["rev"]})
                elif url.path == "/api/undo":
                    self.json({"rev": P.undo(state.path)})
                elif url.path == "/api/render":
                    self.json(state.start_render(bool(data.get("preview", True))))
                else:
                    self.fail("not found", 404)
            except EditError as exc:
                self.fail(str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def upload(self, name: str):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_UPLOAD:
                return self.fail("empty or oversized upload")
            safe = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .") or "upload"
            media = P.load(state.path).dir / "media"
            media.mkdir(exist_ok=True)
            dest, k = media / safe, 2
            while dest.exists():
                dest = media / f"{Path(safe).stem}-{k}{Path(safe).suffix}"
                k += 1
            with open(dest, "wb") as fh:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    fh.write(chunk)
                    left -= len(chunk)
            if left:
                dest.unlink(missing_ok=True)
                return self.fail("upload was cut off")
            try:
                results, _ = ops.apply(state.path, [{"op": "import_asset", "path": str(dest)}])
            except EditError:
                dest.unlink(missing_ok=True)  # not media; do not leave junk behind
                raise
            self.json(results[0])

    return Handler


def serve(project_file: Path, port: int = 8765, open_browser: bool = True) -> None:
    state = State(project_file)
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = Server(("127.0.0.1", p), make_handler(state, p))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise EditError(f"no free port between {port} and {port + 19}; use --port")
    url = f"http://127.0.0.1:{port}/"
    print(f"vedit editor for {project_file.parent.name}: {url}  (Ctrl+C to stop)", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
