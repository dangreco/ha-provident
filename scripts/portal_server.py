# SPDX-License-Identifier: GPL-3.0-only

"""Fake portal over TLS, run inside a container by verify_container.py."""

import sys
import threading

sys.path.insert(0, "/srv")

from fixtures.portal import make_portal

portal, server, thread = make_portal(
    "0.0.0.0",
    443,
    ("/certs/portal.pem", "/certs/portal.key"),
)
print("portal listening", flush=True)
threading.Event().wait()
