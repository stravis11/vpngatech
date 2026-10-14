#!/usr/bin/env python3
"""Stand-in for `openconnect --authenticate` that walks through a GT-style Duo login.

Password "bad" fails the login; password "ask" adds an unexpected prompt.
"""

import os
import sys
import time

err = sys.stderr
password = sys.stdin.readline().rstrip("\n")
err.write("Connected to 128.61.110.1:443\nSSL negotiation with dc-ext-gw.vpn.gatech.edu\n")
err.flush()
time.sleep(0.2)
if password == "bad":
    err.write("Login failed. Authentication failed.\nPassword: ")
    err.flush()
    sys.stdin.readline()
    sys.exit(1)
if password == "ask":
    err.write("Pick a color [red/blue]: ")
    err.flush()
    if sys.stdin.readline().strip() != "blue":
        sys.exit(1)
err.write("Enter a passcode or 'push' or 'phone'\nChallenge: ")
err.flush()
answer = sys.stdin.readline().strip()
if answer not in ("push", "phone", "123456"):
    err.write("Login failed.\n")
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_DELAY", "0.5")))
err.write("GlobalProtect login returned authentication-source=Duo\n")
print("COOKIE='authcookie=abc&portal=GATECH&user=gburdell3'")
print("HOST='128.61.110.1'")
print("CONNECT_URL='https://dc-ext-gw.vpn.gatech.edu'")
print("FINGERPRINT='pin-sha256:AAAA'")
print("RESOLVE='dc-ext-gw.vpn.gatech.edu:128.61.110.1'")
