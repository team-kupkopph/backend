#!/usr/bin/env python3
"""US-E3 · post-deploy smoke suite (§16.3 "smoke tests", §16.6 "smoke suite green").

    python dev/smoke.py https://api.kupkop.ph
    python dev/smoke.py http://localhost:8000 --no-throttle-check

Runs against a DEPLOYED base URL over real HTTP. It is deliberately NOT part of the pytest
suite: pytest proves the code is correct, and this proves that *this deployment* of it is
configured correctly — which is a different question with different failure modes. Every
check here is something that can be green in CI and broken in production: a missing
environment variable, DEBUG left on, a security header dropped by a proxy, a throttle
backend that is not reachable.

Read-only by design, with one deliberate exception (the auth-throttle check, which spends
one login-throttle bucket for the calling IP). Nothing here writes application data, so it
is safe to run against production immediately after a deploy — which is the only moment it
is actually worth running.
"""
from __future__ import annotations

import argparse
import sys
import uuid

import requests

TIMEOUT = 15

# §12.5 · the keys that carry a precise position. `approx_location` is the coarsened pin the
# API is SUPPOSED to publish, so its contents are exempt — flagging it would make this check
# fail on every healthy deployment, and a check that always fails gets switched off.
PRECISE_KEYS = {"lat", "lng", "latitude", "longitude", "geom", "coordinates"}
EXEMPT_PARENTS = {"approx_location"}


def find_precise_coordinates(payload, _path="", _exempt=False) -> list[str]:
    """Every path in `payload` that looks like a precise coordinate. Never raises.

    Recursive on purpose: the rescue map returns `{"reports": [{...}, ...]}`, so a
    top-level-only check would inspect nothing and report "clean" for every payload this
    suite exists to look at.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{_path}.{key}" if _path else key
            exempt = _exempt or key in EXEMPT_PARENTS
            if key in PRECISE_KEYS and value is not None and not exempt:
                # Report the OUTERMOST hit and stop: if `geom` leaked, the whole subtree
                # under it leaked, and listing `geom`, `geom.lat`, `geom.lng` separately
                # triples the noise without adding a fact. One path per leak is what
                # someone reading this at 2am can act on.
                found.append(path)
                continue
            found.extend(find_precise_coordinates(value, path, exempt))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            found.extend(find_precise_coordinates(item, f"{_path}[{i}]", _exempt))
    return found


class Smoke:
    def __init__(self, base: str, throttle_check: bool = True, behind_proxy: bool = False):
        self.base = base.rstrip("/")
        self.throttle_check = throttle_check
        self.session = requests.Session()
        # §12.4 sets SECURE_SSL_REDIRECT, so the app 301s every plain-HTTP request to
        # https. In production that never bites: the ALB terminates TLS and forwards with
        # `X-Forwarded-Proto: https`, which SECURE_PROXY_SSL_HEADER tells Django to trust.
        # Probing the ORIGIN directly over plain HTTP means being that proxy — so
        # --behind-proxy sends exactly the header the ALB sends, rather than the suite
        # pretending the redirect is a failure.
        self.behind_proxy = behind_proxy
        if behind_proxy:
            self.session.headers["X-Forwarded-Proto"] = "https"
        self.results: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = ""):
        self.results.append((bool(ok), name, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))

    def get(self, path, **kw):
        return self.session.get(f"{self.base}{path}", timeout=TIMEOUT, **kw)

    def post(self, path, **kw):
        return self.session.post(f"{self.base}{path}", timeout=TIMEOUT, **kw)

    def https_redirect(self):
        """Answered before anything else, because getting it wrong makes every other check
        fail with an unrelated-looking SSL error.

        A plain-HTTP request that 301s to https is §12.4 working. Following that redirect
        to an origin with no certificate produces WRONG_VERSION_NUMBER seven times over,
        which reads like seven broken endpoints instead of one correct setting.
        """
        if self.base.startswith("https://"):
            return True
        r = self.session.get(f"{self.base}/api/v1/health", timeout=TIMEOUT,
                             allow_redirects=False)
        if r.status_code in (301, 308) and r.headers.get("location", "").startswith("https"):
            if self.behind_proxy:
                self.check(False, "HTTPS redirect", "still redirecting despite "
                                                    "X-Forwarded-Proto: https")
                return False
            self.check(True, "HTTPS redirect is enforced (§12.4 SECURE_SSL_REDIRECT)",
                       "re-run against the https:// URL, or pass --behind-proxy to probe "
                       "this origin the way the ALB does")
            return False
        return True

    # ── the checks ──────────────────────────────────────────────────────────────────
    def health(self):
        r = self.get("/api/v1/health")
        self.check(r.status_code == 200 and r.json().get("status") == "ok",
                   "health endpoint answers", f"HTTP {r.status_code}")

    def public_reads(self):
        for path, key in [("/api/v1/reports/map?city=Manila&radius_km=10", "reports"),
                          ("/api/v1/listings", "results"),
                          ("/api/v1/stories", "results")]:
            r = self.get(path)
            body = r.json() if r.ok else {}
            self.check(r.status_code == 200 and key in body,
                       f"GET {path.split('?')[0]}", f"HTTP {r.status_code}")

    def coordinates_withheld(self):
        """§12.5 against the real deployment.

        This is the check worth running even if every other one is dropped. The rule is
        enforced in serializer code that pytest already covers — but a deployment can still
        leak it through a stale image, a feature flag, or a route pointing at an older
        build, and a coordinate leak is not a bug you get to fix quietly afterwards.
        """
        r = self.get("/api/v1/reports/map?city=Manila&radius_km=10")
        leaks = find_precise_coordinates(r.json() if r.ok else {})
        self.check(not leaks, "rescue map withholds precise coordinates (§12.5)",
                   f"LEAKED: {leaks[:5]}" if leaks else "")

    def writes_need_auth(self):
        r = self.post("/api/v1/reports", json={
            "report_type": "stray", "species": "dog", "condition": "healthy",
            "lat": 14.6, "lng": 121.0, "idempotency_key": uuid.uuid4().hex})
        self.check(r.status_code in (401, 403), "unauthenticated write is refused",
                   f"HTTP {r.status_code}")

    def debug_is_off(self):
        """A deployment running with DEBUG on serves its settings, its SQL and its traceback
        to anyone who can provoke a 404. Settings now refuse to start without a real
        SECRET_KEY when DEBUG is off (US-K1), but a deployment can still be started WITH
        DJANGO_DEBUG=1 by accident — which is exactly the mistake nobody notices, because
        everything works."""
        r = self.get(f"/api/v1/does-not-exist-{uuid.uuid4().hex}")
        body = r.text[:4000].lower()
        tells = [t for t in ("djangosettings", "traceback", "using the urlconf",
                             "django settings", "debug = true") if t in body]
        self.check(not tells, "DEBUG is off (404 is not a debug page)",
                   f"debug page markers: {tells}" if tells else f"HTTP {r.status_code}")

    def security_headers(self):
        """§12.4's header set, checked at the edge rather than in settings.

        Django emits these; a misconfigured proxy or ALB can strip them, and settings tests
        cannot see that. HSTS is only meaningful over HTTPS, so it is required only there.
        """
        r = self.get("/api/v1/health")
        want = {"x-content-type-options": "nosniff", "referrer-policy": None,
                "x-frame-options": None}
        if self.base.startswith("https://"):
            want["strict-transport-security"] = None
        missing = [h for h, v in want.items()
                   if h not in r.headers or (v and r.headers[h].lower() != v)]
        self.check(not missing, "§12.4 security headers present",
                   f"missing/wrong: {missing}" if missing else "")

    def auth_throttle_live(self):
        """§16.6 · "Rate limiting on OTP & auth" — proven, not assumed.

        Throttling depends on a working cache backend. If that is misconfigured the app
        does not fail: it serves every request happily, unthrottled, and looks perfectly
        healthy right up until someone notices. The only way to know is to trip it.

        ⚠️ This spends one login-throttle bucket for the calling IP (a CI runner), and
        deliberately uses an address that cannot exist. `--no-throttle-check` skips it.
        """
        if not self.throttle_check:
            self.check(True, "auth throttling (skipped)", "--no-throttle-check")
            return
        email = f"smoke-{uuid.uuid4().hex}@kupkop.invalid"
        codes = []
        for _ in range(25):
            r = self.post("/api/v1/auth/login",
                          json={"email": email, "password": "not-a-real-password"})
            codes.append(r.status_code)
            if r.status_code == 429:
                envelope = r.json().get("error", {})
                self.check(envelope.get("code") == "throttled",
                           "throttle returns the documented error envelope",
                           f"got {envelope.get('code')!r}")
                break
        tripped = 429 in codes
        self.check(tripped, "auth throttling is live",
                   f"tripped after {codes.index(429) + 1} attempts" if tripped else
                   f"NO 429 in {len(codes)} attempts — is the cache backend reachable?")

    def run(self) -> int:
        print(f"Smoke suite → {self.base}"
              f"{'  (as the proxy: X-Forwarded-Proto: https)' if self.behind_proxy else ''}\n")
        if not self.https_redirect():
            # Every subsequent check would fail for the same single reason. Reporting that
            # reason once beats reporting it seven times as seven different endpoints.
            print("\nStopping: the remaining checks would all fail for this one reason.")
            return 1
        for step in (self.health, self.public_reads, self.coordinates_withheld,
                     self.writes_need_auth, self.debug_is_off, self.security_headers,
                     self.auth_throttle_live):
            try:
                step()
            except Exception as exc:                        # noqa: BLE001
                # An exception in a smoke check is a failed check, never a crashed run —
                # the remaining checks still carry information about what else is broken.
                self.check(False, step.__name__, f"{type(exc).__name__}: {exc}")
        failed = [name for ok, name, _ in self.results if not ok]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed.")
        if failed:
            print("FAILED: " + ", ".join(failed), file=sys.stderr)
            return 1
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="e.g. https://api.kupkop.ph")
    ap.add_argument("--no-throttle-check", action="store_true",
                    help="skip the login-throttle probe (it spends one bucket for this IP)")
    ap.add_argument("--behind-proxy", action="store_true",
                    help="send X-Forwarded-Proto: https, as the ALB does — for probing an "
                         "origin directly over plain HTTP")
    args = ap.parse_args(argv)
    return Smoke(args.base_url, throttle_check=not args.no_throttle_check,
                 behind_proxy=args.behind_proxy).run()


if __name__ == "__main__":
    raise SystemExit(main())
