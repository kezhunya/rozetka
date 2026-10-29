"""Compare complete Rozetka XML while ignoring only its generation timestamp."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


YML_DATE = re.compile(rb'(<yml_catalog\b[^>]*\bdate=")[^"]*(")')
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def semantic_sha256(stream) -> str:
    digest = hashlib.sha256()
    normalized_date = False
    for line in stream:
        if not normalized_date and b"<yml_catalog" in line:
            line, count = YML_DATE.subn(lambda m: m.group(1) + b"_" + m.group(2), line, count=1)
            if count != 1:
                raise ValueError("XML has no expected yml_catalog date attribute")
            normalized_date = True
        digest.update(line)
    if not normalized_date:
        raise ValueError("XML has no yml_catalog root")
    return digest.hexdigest()


def previous_digest(state: dict, public_url: str) -> str | None:
    saved = state.get("semantic_sha256")
    if isinstance(saved, str) and SHA256.fullmatch(saved):
        return saved
    try:
        request = Request(public_url, headers={"Accept": "application/xml", "User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=120) as response:
            return semantic_sha256(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rendered", type=Path, required=True)
    parser.add_argument("--previous-state", type=Path, required=True)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    state = json.loads(args.previous_state.read_text(encoding="utf-8"))
    with args.rendered.open("rb") as stream:
        current = semantic_sha256(stream)
    old = previous_digest(state, args.public_url)
    result = {"changed": old != current, "semantic_sha256": current,
              "previous_semantic_sha256": old}
    args.output.write_text(json.dumps(result), encoding="utf-8")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write("changed=" + str(result["changed"]).lower() + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
