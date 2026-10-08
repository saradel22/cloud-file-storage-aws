import os
import re

import boto3
from authlib.integrations.flask_client import OAuth
from botocore.config import Config
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template_string, request, session, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-only-change-me")

# ---- Google OAuth 2.0 / OpenID Connect login ----
oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---- AWS S3 ----
BUCKET = os.getenv("S3_BUCKET")
REGION = os.getenv("AWS_REGION", "ap-south-1")
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))

MAX_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED = {"pdf", "png", "jpg", "jpeg", "txt", "docx", "csv"}


def logged_in():
    return "user" in session


def user_prefix():
    # every user only ever sees their own folder: users/<email>/
    return "users/" + session["user"]["email"] + "/"


def owns(key):
    return key.startswith(user_prefix())


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


PAGE = """
<!doctype html>
<title>Cloud File Storage</title>
<body style="font-family:Arial;max-width:850px;margin:30px auto;padding:0 12px">
<h2>Cloud File Storage</h2>
{% if not user %}
  <p>Secure file storage on AWS S3 with Google login, file versioning and expiring share links.</p>
  <a href="/login"><button style="padding:10px 18px">Login with Google</button></a>
{% else %}
  <p>Logged in as <b>{{ user.email }}</b> | <a href="/logout">Logout</a></p>
  {% for m in get_flashed_messages() %}<p style="color:green">{{ m }}</p>{% endfor %}
  <form method="post" action="/upload" enctype="multipart/form-data">
    <input type="file" name="file" required>
    <button>Upload</button>
  </form>
  <p style="color:#666;font-size:13px">Allowed: pdf, png, jpg, jpeg, txt, docx, csv. Max 10 MB.
  Uploading a file with the same name creates a new version.</p>
  <h3>Your files</h3>
  <table border="1" cellpadding="6" style="border-collapse:collapse;width:100%">
  <tr><th>File</th><th>Versions</th><th>Share (1 hour)</th><th>Delete</th></tr>
  {% for f in files %}
  <tr>
    <td>{{ f.name }}</td>
    <td>
      {% for v in f.versions %}
        <div>v{{ v.number }} - {{ v.modified }} {% if v.latest %}<b>(latest)</b>{% endif %}
          <a href="/download?key={{ f.key|urlencode }}&version={{ v.id|urlencode }}">download</a>
          {% if not v.latest %}<a href="/restore?key={{ f.key|urlencode }}&version={{ v.id|urlencode }}">restore</a>{% endif %}
        </div>
      {% endfor %}
    </td>
    <td><a href="/share?key={{ f.key|urlencode }}">Create link</a></td>
    <td><a href="/delete?key={{ f.key|urlencode }}">Delete</a></td>
  </tr>
  {% else %}
  <tr><td colspan="4">No files yet.</td></tr>
  {% endfor %}
  </table>
{% endif %}
</body>
"""


@app.route("/")
def index():
    if not logged_in():
        return render_template_string(PAGE, user=None)
    files = {}
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=user_prefix()):
        for v in page.get("Versions", []):
            key = v["Key"]
            entry = files.setdefault(
                key, {"key": key, "name": key[len(user_prefix()):], "versions": []}
            )
            entry["versions"].append(
                {
                    "id": v["VersionId"],
                    "modified": v["LastModified"].strftime("%d %b %Y %H:%M:%S"),
                    "latest": v["IsLatest"],
                    "ts": v["LastModified"],
                }
            )
    for entry in files.values():
        entry["versions"].sort(key=lambda x: x["ts"])
        for i, v in enumerate(entry["versions"], start=1):
            v["number"] = i
        entry["versions"].reverse()  # newest first
    return render_template_string(PAGE, user=session["user"], files=list(files.values()))


@app.route("/login")
def login():
    return google.authorize_redirect(url_for("auth", _external=True))


@app.route("/auth")
def auth():
    token = google.authorize_access_token()
    info = token.get("userinfo") or {}
    session["user"] = {"email": info.get("email")}
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/upload", methods=["POST"])
def upload():
    if not logged_in():
        return redirect("/")
    f = request.files["file"]
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED:
        flash("File type not allowed")
        return redirect("/")
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_SIZE:
        flash("File too large (max 10 MB)")
        return redirect("/")
    s3.upload_fileobj(f, BUCKET, user_prefix() + safe_name(f.filename))
    flash("Uploaded " + f.filename)
    return redirect("/")


@app.route("/download")
def download():
    if not logged_in():
        return redirect("/")
    key, version = request.args["key"], request.args["version"]
    if not owns(key):
        return "Forbidden", 403
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key, "VersionId": version},
        ExpiresIn=300,
    )
    return redirect(url)


@app.route("/restore")
def restore():
    if not logged_in():
        return redirect("/")
    key, version = request.args["key"], request.args["version"]
    if not owns(key):
        return "Forbidden", 403
    s3.copy_object(
        Bucket=BUCKET,
        Key=key,
        CopySource={"Bucket": BUCKET, "Key": key, "VersionId": version},
    )
    flash("Old version restored as the new latest version")
    return redirect("/")


@app.route("/share")
def share():
    if not logged_in():
        return redirect("/")
    key = request.args["key"]
    if not owns(key):
        return "Forbidden", 403
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600
    )
    return (
        "<body style='font-family:Arial;max-width:850px;margin:30px auto'>"
        "<p>Share link (expires in 1 hour):</p>"
        f"<textarea rows='7' cols='90'>{url}</textarea>"
        "<p><a href='/'>Back</a></p></body>"
    )


@app.route("/delete")
def delete():
    if not logged_in():
        return redirect("/")
    key = request.args["key"]
    if not owns(key):
        return "Forbidden", 403
    s3.delete_object(Bucket=BUCKET, Key=key)
    flash("Deleted (S3 keeps a delete marker, so older versions are recoverable)")
    return redirect("/")


if __name__ == "__main__":
    app.run(port=5000, debug=True)
