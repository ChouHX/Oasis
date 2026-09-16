#!/usr/bin/env python3
"""Outlook (MSA) mail reader over Microsoft Graph.

Credential line format:  email----password----client_id----refresh_token
MSA refresh tokens must be redeemed against the /consumers tenant.
"""
import json, sys, time, urllib.parse, urllib.request

CLIENT_ID_DEFAULT = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "https://graph.microsoft.com/.default"


def parse_cred(line):
    parts = line.strip().split("----")
    if len(parts) < 4:
        raise ValueError("expected email----password----client_id----refresh_token")
    return {"email": parts[0], "password": parts[1],
            "client_id": parts[2] or CLIENT_ID_DEFAULT, "refresh_token": parts[3]}


def post_form(url, data, proxy=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(req, timeout=45) as r:
        return json.loads(r.read().decode())


def refresh(cred, proxy=None):
    """Returns dict with access_token / refresh_token."""
    return post_form(TOKEN_URL, {
        "client_id": cred["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": cred["refresh_token"],
        "scope": SCOPES,
    }, proxy)


def graph_get(path, token, proxy=None):
    req = urllib.request.Request(GRAPH + path,
                                headers={"Authorization": "Bearer " + token,
                                         "Accept": "application/json"})
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(req, timeout=45) as r:
        return json.loads(r.read().decode())


def main():
    line = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/oasis_cred.txt").read()
    proxy = sys.argv[2] if len(sys.argv) > 2 else None
    cred = parse_cred(line)
    print("email:", cred["email"], "client:", cred["client_id"])
    tok = refresh(cred, proxy)
    print("token keys:", sorted(tok.keys()))
    at = tok["access_token"]
    if "refresh_token" in tok:
        print("NEW REFRESH TOKEN:", tok["refresh_token"][:60], "...")
    msgs = graph_get("/me/messages?$top=8&$select=subject,from,receivedDateTime,bodyPreview", at, proxy)
    for m in msgs.get("value", []):
        print(" -", m["receivedDateTime"], "|", (m.get("from") or {}).get("emailAddress", {}).get("address"),
              "|", m["subject"][:80])


if __name__ == "__main__":
    main()
