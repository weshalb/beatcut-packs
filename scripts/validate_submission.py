"""Validate submissions from GitHub issue events and update the central index."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LIMIT = 2_000_000_000
BLOCK = 1024 * 1024
FIELDS = frozenset("""
id name author github_user tags clip_count total_seconds size_bytes sha256
zip_url cover_url preview_urls created
""".split())


class InvalidSubmission(ValueError):
    """The issue data failed a submission requirement."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSubmission("The JSON contains a duplicate key.")
        result[key] = value
    return result


def parse_submission(body: str | None, issue_author: str,
                     existing_ids: set[str]) -> dict[str, Any]:
    """Parse the issue JSON and validate metadata and release URL structure."""
    blocks = re.findall(r"^```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?$",
                        body or "", flags=re.MULTILINE | re.DOTALL)
    if len(blocks) != 1:
        raise InvalidSubmission("Include exactly one fenced JSON block.")
    try:
        entry = json.loads(blocks[0], object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise InvalidSubmission(f"Invalid JSON: {exc}") from exc
    if not isinstance(entry, dict) or entry.keys() != FIELDS:
        raise InvalidSubmission("The JSON must contain exactly the index entry fields.")
    for field in ("id", "name", "author", "github_user", "sha256", "created"):
        if not isinstance(entry[field], str) or not entry[field].strip():
            raise InvalidSubmission(f"{field} must be nonempty text.")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}", entry["id"]) or len(entry["id"]) > 94:
        raise InvalidSubmission("Invalid pack ID.")
    slug = entry["id"].rsplit("-", 1)[0]
    if re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", slug):
        raise InvalidSubmission("The pack ID is reserved on Windows.")
    if entry["id"] in existing_ids:
        raise InvalidSubmission("This pack ID is already in the index.")
    if (not re.fullmatch(r"[A-Za-z0-9-]{1,39}", entry["github_user"])
            or issue_author.casefold() != entry["github_user"].casefold()):
        raise InvalidSubmission("github_user must match the issue author.")
    if not isinstance(entry["tags"], list) or any(not isinstance(tag, str) for tag in entry["tags"]):
        raise InvalidSubmission("tags must be a list of strings.")
    if type(entry["clip_count"]) is not int or entry["clip_count"] < 1:
        raise InvalidSubmission("clip_count must be a positive integer.")
    if type(entry["size_bytes"]) is not int or not 0 < entry["size_bytes"] <= LIMIT:
        raise InvalidSubmission("size_bytes must be positive and at most 2 GB.")
    duration = entry["total_seconds"]
    if type(duration) not in (int, float) or not 0 <= duration <= sys.float_info.max:
        raise InvalidSubmission("total_seconds must be finite and nonnegative.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"]):
        raise InvalidSubmission("sha256 must have 64 hexadecimal digits.")
    try:
        date = datetime.fromisoformat(entry["created"])
        if date.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as exc:
        raise InvalidSubmission("created must be an ISO timestamp with a timezone.") from exc
    previews = entry["preview_urls"]
    if not isinstance(previews, list) or len(previews) != 12:
        raise InvalidSubmission("Provide 12 preview URLs.")
    urls = [entry["zip_url"], entry["cover_url"], *previews]
    base = rf"https://github\.com/{re.escape(issue_author)}/([A-Za-z0-9_.-]+)/releases/download/[A-Za-z0-9_.%+-]+/"
    locations = []
    for url, extension in zip(urls, [r"zip", *([r"jpe?g"] * 13)], strict=True):
        if not isinstance(url, str) or not re.fullmatch(
                base + rf"[A-Za-z0-9_.%+-]+\.{extension}", url, re.IGNORECASE):
            raise InvalidSubmission("Every URL must be a GitHub release asset of the issue author.")
        parts = urlsplit(url).path.split("/")
        if any(part in {".", ".."} or "%" in part for part in (parts[2], parts[5], parts[6])):
            raise InvalidSubmission("Release URLs must use plain repository, tag and asset names.")
        locations.append(tuple(parts[1:6]))
    if len(set(locations)) != 1 or len({url.casefold() for url in urls}) != len(urls):
        raise InvalidSubmission("Provide distinct assets from a single GitHub release.")
    return entry


class _AssetRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        location = urlsplit(newurl)
        if location.scheme != "https" or location.netloc not in {
                "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                "github-releases.githubusercontent.com"}:
            raise InvalidSubmission("GitHub redirected outside its release asset hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_assets(entry: dict[str, Any]) -> None:
    """Stream and hash the ZIP and decode all JPEG assets."""
    import cv2
    import numpy as np

    opener = build_opener(_AssetRedirect())

    def request(url: str) -> Any:
        return opener.open(Request(url, headers={"User-Agent": "Beatcut-Packstore/1"}),
                           timeout=30)

    digest, length = hashlib.sha256(), 0
    with request(entry["zip_url"]) as stream:
        declared = stream.headers.get("Content-Length")
        if declared and int(declared) != entry["size_bytes"]:
            raise InvalidSubmission("ZIP Content-Length differs from size_bytes.")
        while chunk := stream.read(BLOCK):
            length += len(chunk)
            if length > min(LIMIT, entry["size_bytes"]):
                raise InvalidSubmission("ZIP exceeds its declared size or the 2 GB limit.")
            digest.update(chunk)
    if length != entry["size_bytes"]:
        raise InvalidSubmission("The downloaded ZIP size differs from size_bytes.")
    if digest.hexdigest() != entry["sha256"].lower():
        raise InvalidSubmission("The downloaded ZIP SHA-256 does not match.")
    for number, url in enumerate([entry["cover_url"], *entry["preview_urls"]]):
        with request(url) as stream:
            data = stream.read(8 * BLOCK + 1)
        if (len(data) > 8 * BLOCK or not data.startswith(b"\xff\xd8")
                or not data.endswith(b"\xff\xd9")):
            raise InvalidSubmission(f"Image {number + 1} is not a supported JPEG.")
        try:
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_REDUCED_COLOR_8)
        except cv2.error as exc:
            raise InvalidSubmission(f"Image {number + 1} could not be decoded.") from exc
        if image is None:
            raise InvalidSubmission(f"Image {number + 1} could not be decoded.")


def _run(*args: str, capture: bool = False) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=capture).stdout or ""


def _label(number: str, label: str, *remove: str) -> None:
    # gh label create --force also handles first-run and concurrent setup.
    _run("gh", "label", "create", label, "--force",
         "--color", "2da44e" if label == "valid" else "cf222e")
    _run("gh", "issue", "edit", number, "--add-label", label)
    for old in remove:
        _run("gh", "api", "--method", "DELETE",
             f"repos/{os.environ['GH_REPO']}/issues/{number}/labels/{old}")


def main() -> None:
    """Process one issue event in the central repository workflow."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    number = str(int(event["issue"]["number"]))
    owner = event["repository"]["owner"]["login"]
    approved = event["action"] == "labeled"
    if approved and (event["label"]["name"] != "approved"
                     or event["sender"]["login"].casefold() != owner.casefold()):
        return
    issue = json.loads(_run("gh", "issue", "view", number, "--json",
                            "title,body,author,labels,state", capture=True))
    if issue["state"] != "OPEN" or not issue["title"].startswith("Pack submission:"):
        return
    labels = {label["name"] for label in issue["labels"]}
    if approved and "approved" not in labels:
        return
    if approved and (issue["body"] != event["issue"]["body"]
                     or issue["title"] != event["issue"]["title"]):
        _run("gh", "issue", "edit", number, "--remove-label", "approved")
        _run("gh", "issue", "comment", number, "--body",
             "The submission changed after approval. Review it and apply approved again.")
        return
    index_path = Path("packs.json")
    branch = event["repository"]["default_branch"]
    verified = False
    for _ in range(5):
        if approved:
            _run("git", "fetch", "origin", branch)
            _run("git", "reset", "--hard", "FETCH_HEAD")
        index = json.loads(index_path.read_bytes())
        try:
            entry = parse_submission(issue["body"], issue["author"]["login"],
                                     {item["id"] for item in index["packs"]})
            if not verified:
                validate_assets(entry)
                verified = True
        except Exception as exc:
            # Report validation and download failures without inserting raw user text as Markdown.
            if not isinstance(exc, (InvalidSubmission, OSError, ValueError)):
                raise
            _label(number, "invalid", *sorted(labels & {"valid", "approved"}))
            detail = str(exc).replace("@", "@\u200b").replace("`", "'").replace("\n", " ")[:700]
            _run("gh", "issue", "comment", number, "--body", f"Submission invalid. `{detail}`")
            return
        if not approved:
            _label(number, "valid", *sorted(labels & {"invalid"}))
            _run("gh", "issue", "comment", number, "--body", "Waiting for approval")
            return
        index["packs"].append(entry)
        index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _run("git", "-c", "user.name=github-actions[bot]",
             "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
             "commit", "-am", f"Approve pack {entry['id']} (#{number})")
        result = subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"])
        if result.returncode == 0:
            _run("gh", "issue", "close", number)
            return
    raise RuntimeError("Failed to push the pack index after five attempts.")


if __name__ == "__main__":
    main()
