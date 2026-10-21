# SPDX-License-Identifier: GPL-3.0-only

"""Fake Provident portal; its clock is ``datetime.now``, so freezing time freezes it too."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")

USERNAME = "demo"
PASSWORD = "hunter2"

TREE = "/api/internal/metertree/rootnodes"
GRAPHS = "/api/internal/graphs/quickgraphs"
REPORT = "/api/internal/reports/quickreport"
LOGIN = "/login/LoginService.aspx/ProcessLogin"
CHECK_LOGIN = "/login/LoginService.aspx/CheckLogin"
DETAILS = "/secure/AccountInfo.aspx/GetDetails"
# Provident's corporate site (pemi.com), served from the same fake host.
RATES_LOGIN = "/do-rates-login"
RATES_PAGE = "/rates-and-fees"

#: The real rates page (trimmed), as served for a signed-in location.
RATES_HTML = (Path(__file__).parent / "rates_page.html").read_text()
#: What the rates site serves without a session.
RATES_SIGN_IN = (
    '<form action="/do-rates-login" method="POST">'
    '<input name="CustomerNumber"><input name="LocationID"></form>'
)

METERS = [
    {
        "id": 658637,
        "node": "MP:658637",
        "name": "3105",
        "utility": "Electricity",
        "unit": "kWh",
        "serial": "85006408-9",
    },
    {
        "id": 692294,
        "node": "MP:692294",
        "name": "3105",
        "utility": "Cold Water",
        "unit": "m3",
        "serial": "23166694-2",
    },
    {
        "id": 692262,
        "node": "MP:692262",
        "name": "3105",
        "utility": "Hot Water",
        "unit": "m3",
        "serial": "24119905-2",
    },
]

# Register odometer at the start of the simulated history.
BASE_REGISTER = {"Electricity": 3901.0, "Cold Water": 109.2, "Hot Water": 92.0, "Gas": 0.0}

# These publish a whole day only after it ends; the rest publish hourly.
DELAYED_UTILITIES = frozenset({"Electricity"})


def page_method(result: object) -> dict[str, str]:
    """ASP.NET envelope: ``d`` holds the JSON as a string."""
    return {"d": json.dumps(result)}


def _meter_nodes(meters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = [
        {
            "id": "GROUP:121497",
            "text": "3105",
            "parent": "#",
            "icon": "fas fa-folder",
            "children": False,
        }
    ]
    for m in meters:
        nodes.append(
            {
                "id": m["node"],
                "text": m["name"],
                "parent": "GROUP:121497",
                "icon": "x",
                "a_attr": {"title": f"{m['name']} [{m['utility']} - {m['unit']}] - {m['serial']}"},
            }
        )
    return nodes


@dataclass
class Portal:
    url: str
    #: Per utility: hour index (0 = origin 00:00) -> consumption; gaps are generated.
    hourly: dict[str, dict[int, float]] = field(default_factory=dict)
    reject_login: str | None = None
    status_error: int | None = None
    origin: date = field(default_factory=lambda: date.today() - timedelta(days=30))
    meters: list[dict[str, Any]] = field(default_factory=lambda: list(METERS))
    #: (method, path, query, body)
    requests: list[tuple[str, str, dict[str, list[str]], bytes]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: Rotating this expires every logged-in client.
    session_token: str = "s1"
    #: Rates page HTML for a signed-in location; rotating ``rates_session`` expires it.
    rates_html: str = RATES_HTML
    rates_session: str = "r1"
    #: Status the rates site answers with instead (simulates an outage), if set.
    rates_status_error: int | None = None

    def hour_value(self, utility: str, day: date, hour: int) -> float:
        idx = (day - self.origin).days * 24 + hour
        with self._lock:
            value = self.hourly.setdefault(utility, {}).get(idx)
        if value is not None:
            return value
        if utility == "Electricity":
            base = 0.15 if hour < 6 else 0.55 if hour < 9 else 0.35
            return round(base + ((day.toordinal() * 31 + hour * 7) % 11) * 0.01, 3)
        if hour < 6 or hour > 21:
            return 0.0
        return round(0.01 + ((day.toordinal() * 17 + hour * 3) % 7) * 0.002, 3)

    def day_value(self, utility: str, day: date) -> float:
        return round(sum(self.hour_value(utility, day, h) for h in range(24)), 4)

    def set_hour(self, utility: str, day: date, hour: int, value: float) -> None:
        idx = (day - self.origin).days * 24 + hour
        with self._lock:
            self.hourly.setdefault(utility, {})[idx] = value

    def series_for(self, utility: str, start: date, end: date, now: datetime) -> list[float]:
        """Hourly buckets for [start, end); unpublished ones are zero."""
        data = []
        day = start
        while day < end:
            for hour in range(24):
                bucket_start = datetime.combine(day, datetime.min.time(), tzinfo=_UTC).replace(
                    hour=hour
                )
                if utility in DELAYED_UTILITIES:
                    published = day < now.date()
                else:
                    published = bucket_start < now.replace(minute=0, second=0, microsecond=0)
                data.append(self.hour_value(utility, day, hour) if published else 0.0)
            day += timedelta(days=1)
        return data

    def register_at(self, utility: str, end: date, today: date) -> float:
        """Odometer at the end of ``end``; today's partial hours are not in it yet."""
        total = BASE_REGISTER[utility]
        day = self.origin
        while day < min(end, today):
            total += self.day_value(utility, day)
            day += timedelta(days=1)
        return round(total, 3)


class _Handler(BaseHTTPRequestHandler):
    portal: Portal

    def _answer(self) -> None:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with self.portal._lock:
            self.portal.requests.append((self.command, url.path, parse_qs(url.query), body))

        if self.portal.status_error is not None:
            self._send(self.portal.status_error, {"Message": "server error"})
            return

        path = url.path
        if path in (RATES_LOGIN, RATES_PAGE):
            self._rates(path, body)
            return
        if path == LOGIN:
            self._login(json.loads(body) if body else {})
            return
        cookie = self.headers.get("Cookie") or ""
        expected = f"ASP.NET_SessionId={self.portal.session_token}"
        if expected not in cookie:
            self._send(401, {"Message": "Authorization has been denied for this request."})
            return
        now = datetime.now(_UTC)
        if path == CHECK_LOGIN:
            self._send(200, {"d": True})
        elif path == TREE:
            self._send(200, _meter_nodes(self.portal.meters))
        elif path == DETAILS:
            self._send(
                200,
                page_method(
                    {
                        "login": USERNAME,
                        "firstName": "Demo",
                        "lastName": "User",
                        "email": "demo@example.com",
                        "groups": ["3105"],
                    }
                ),
            )
        elif path == GRAPHS:
            self._graphs(parse_qs(url.query), now)
        elif path == REPORT:
            self._report(parse_qs(url.query), now)
        else:
            self._send(404, {"Message": "not found"})

    def _login(self, data: dict[str, Any]) -> None:
        ok = data.get("username") == USERNAME and data.get("password") == PASSWORD
        if self.portal.reject_login is not None:
            self._send(200, page_method({"success": False, "msg": self.portal.reject_login}))
        elif ok:
            self._send(200, page_method({"success": True}))
        else:
            self._send(200, page_method({"success": False, "msg": "Invalid Username or Password"}))

    def _rates(self, path: str, body: bytes) -> None:
        if self.portal.rates_status_error is not None:
            self._send_text(self.portal.rates_status_error, "<p>Service unavailable</p>")
            return
        if path == RATES_LOGIN:
            # Only the location (the portal username) is checked, like the real site.
            form = parse_qs(body.decode())
            ok = form.get("LocationID") == [USERNAME] and bool(form.get("CustomerNumber"))
            payload = json.dumps(
                {"response": "ok", "message": "", "error": 0}
                if ok
                else {"response": "no", "message": "Invalid login.", "error": 0}
            )
            cookie = f"PHPSESSID={self.portal.rates_session}; Path=/" if ok else None
            self._send_text(200, payload, "application/json", cookie)
            return
        cookie = self.headers.get("Cookie") or ""
        signed_in = f"PHPSESSID={self.portal.rates_session}" in cookie
        self._send_text(200, self.portal.rates_html if signed_in else RATES_SIGN_IN)

    def _send_text(
        self,
        status: int,
        text: str,
        content_type: str = "text/html; charset=UTF-8",
        cookie: str | None = None,
    ) -> None:
        payload = text.encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _graphs(self, query: dict[str, list[str]], now: datetime) -> None:
        start = date.fromisoformat(query["startDate"][0])
        end = date.fromisoformat(query["endDate"][0])
        nodes = query["meterlist"][0].split(",")
        result: list[dict[str, Any]] = []
        for node in nodes:
            meter = next(m for m in self.portal.meters if m["node"] == node)
            data = self.portal.series_for(meter["utility"], start, end, now)
            result.append(
                {
                    "utility": f"{meter['utility']} ({meter['unit']})",
                    "units": meter["unit"],
                    "name": f"{meter['name']}|{meter['name']}|Tower",
                    "site": "Tower",
                    "data": data,
                }
            )
        self._send(200, result)

    def _report(self, query: dict[str, list[str]], now: datetime) -> None:
        start = date.fromisoformat(query["startDate"][0])
        end = date.fromisoformat(query["endDate"][0])
        result: dict[str, list[dict[str, Any]]] = {}
        for m in self.portal.meters:
            prev = self.portal.register_at(m["utility"], start, now.date())
            cur = self.portal.register_at(m["utility"], end, now.date())
            usable = end <= now.date()
            row = {
                "Name": m["name"],
                "Meter #": m["serial"],
                "Previous": prev,
                "Current": cur if usable else None,
                "Total": round(cur - prev, 3) if usable else None,
                "Daily Average": round((cur - prev) / max((end - start).days, 1), 3)
                if usable
                else None,
                "ID": m["id"],
                "Error": not usable,
            }
            result.setdefault(f"{m['utility']} ({m['unit']})", []).append(row)
        self._send(200, result)

    def _send(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        if self.path == LOGIN and status == 200:
            self.send_header(
                "Set-Cookie",
                f"ASP.NET_SessionId={self.portal.session_token}; Path=/",
            )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer

    def log_message(self, *args: object) -> None:
        pass


def make_portal(
    host: str = "127.0.0.1",
    port: int = 0,
    tls: tuple[str, str] | None = None,
) -> tuple[Portal, ThreadingHTTPServer, threading.Thread]:
    """Start a fake portal on a free loopback port; ``tls`` is (certfile, keyfile) for HTTPS."""
    portal = Portal(url="")

    class Handler(_Handler):
        pass

    Handler.portal = portal
    server = ThreadingHTTPServer((host, port), Handler)
    scheme = "http"
    if tls is not None:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    portal.url = f"{scheme}://{host}:{server.server_address[1]}/"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
        name="fake-portal",
    )
    thread.start()
    return portal, server, thread
