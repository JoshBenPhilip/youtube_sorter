import os
from pathlib import Path

# Load settings like ANTHROPIC_API_KEY from a .env file next to this script, if present.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import pipeline  # noqa: E402  (must import after .env is loaded)

app = Flask(__name__)


def _mark_interrupted_jobs():
    # Jobs run in background threads, so a restart leaves them stuck "in progress".
    for c in pipeline.list_channels():
        if c["status"] in ("listing", "summarizing", "organizing"):
            pipeline.update_channel(c["slug"], lambda d: d.update(
                status="error", error="Interrupted by a server restart. Click Re-run; finished videos are kept."))


@app.get("/")
def index():
    return render_template("index.html", channels=pipeline.list_channels(),
                           has_key=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/start")
def start():
    url = request.form.get("url", "").strip()
    if not url:
        return redirect(url_for("index"))
    limit = request.form.get("limit", "").strip()
    slug = pipeline.start_job(url, int(limit) if limit.isdigit() and int(limit) > 0 else None)
    return redirect(url_for("channel", slug=slug))


@app.post("/channel/<slug>/rerun")
def rerun(slug):
    data = pipeline.load_channel(slug) or abort(404)
    pipeline.start_job(data["url"], data.get("limit"))
    return redirect(url_for("channel", slug=slug))


@app.get("/channel/<slug>")
def channel(slug):
    pipeline.load_channel(slug) or abort(404)
    return render_template("channel.html", slug=slug)


@app.get("/api/channel/<slug>")
def channel_json(slug):
    return jsonify(pipeline.load_channel(slug) or abort(404))


@app.get("/channel/<slug>/export.md")
def export_md(slug):
    data = pipeline.load_channel(slug) or abort(404)
    if data["status"] != "done":
        abort(409)
    return Response(pipeline.to_markdown(data), mimetype="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename={slug}-course.md"})


@app.get("/channel/<slug>/export.json")
def export_json(slug):
    data = pipeline.load_channel(slug) or abort(404)
    return Response(jsonify(data).get_data(), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={slug}-course.json"})


_mark_interrupted_jobs()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
