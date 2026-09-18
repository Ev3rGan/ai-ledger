from __future__ import annotations

import time
from http.client import HTTPConnection
from ipaddress import ip_address


def request(host: str, path: str, *, anonymous_header: str | None = None):
    headers = {"Host": host}
    if anonymous_header is not None:
        headers["X-AI-Anonymous-Client"] = anonymous_header
    connection = HTTPConnection("edge", 80, timeout=2)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        connection.close()


for attempt in range(30):
    try:
        if request("public.invalid", "/")[0] == 200:
            break
    except OSError:
        pass
    time.sleep(0.5)
else:
    raise AssertionError("Caddy did not become ready")

status, headers, body = request(
    "public.invalid",
    "/assets/public-hash.js",
    anonymous_header="spoofed",
)
assert status == 200
assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
assert body and "spoofed" not in body
ip_address(body)

for path, expected_status in (
    ("/assets/missing-hash.js", 404),
    ("/assets/failure-hash.js", 500),
):
    status, headers, _ = request("public.invalid", path)
    assert status == expected_status
    assert headers.get("Cache-Control") != "public, max-age=31536000, immutable"

assert request("public.invalid", "/operator")[0] == 404
assert request("operator.invalid", "/browse")[0] == 404

status, headers, body = request(
    "operator.invalid",
    "/operator",
    anonymous_header="spoofed",
)
assert status == 200
assert headers["Cache-Control"] == "no-store"
assert body == ""
