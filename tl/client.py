"""Minimal client library + CLI for the transparent log service.

Uses only urllib (stdlib).  Also performs *local* verification of
inclusion proofs, consistency proofs and STH signatures: a client must
never trust a proof it cannot check against a root it authenticated.
"""

from __future__ import annotations

import argparse
import base64
import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import merkle
from .signing import verify_sth_signature


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


class LogClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8088", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- API ----------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self._get("/health")

    def add(self, data: bytes) -> int:
        return self._post("/v1/entries", {"data_b64": b64(data)})["index"]

    def add_text(self, text: str) -> int:
        return self._post("/v1/entries", {"text": text})["index"]

    def entry(self, index: int) -> Dict[str, Any]:
        return self._get("/v1/entries", {"index": index})

    def sth(self) -> Dict[str, Any]:
        return self._get("/v1/sth")

    def public_key(self) -> Dict[str, Any]:
        return self._get("/v1/public-key")

    def inclusion(self, leaf_index: int, tree_size: Optional[int] = None) -> Dict[str, Any]:
        q = {"leaf_index": leaf_index}
        if tree_size is not None:
            q["tree_size"] = tree_size
        return self._get("/v1/proof/inclusion", q)

    def consistency(self, old_size: int, new_size: Optional[int] = None) -> Dict[str, Any]:
        q = {"old_size": old_size}
        if new_size is not None:
            q["new_size"] = new_size
        return self._get("/v1/proof/consistency", q)

    def verify_server(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/v1/verify", body)

    # -- local verification -------------------------------------------------

    @staticmethod
    def verify_inclusion_response(resp: Dict[str, Any]) -> bool:
        return merkle.verify_inclusion(
            resp["leaf_index"],
            resp["tree_size"],
            unb64(resp["leaf_hash"]),
            [unb64(h) for h in resp["proof"]],
            unb64(resp["root_hash"]),
        )

    @staticmethod
    def verify_consistency_response(resp: Dict[str, Any]) -> bool:
        return merkle.verify_consistency(
            resp["old_size"],
            resp["new_size"],
            unb64(resp["old_root"]),
            unb64(resp["new_root"]),
            [unb64(h) for h in resp["proof"]],
        )

    def verify_sth(self, sth: Dict[str, Any]) -> bool:
        pub = unb64(self.public_key()["public_key_b64"])
        ok, _ = verify_sth_signature(
            pub,
            sth["tree_size"],
            unb64(sth["root_hash"]),
            sth["timestamp_us"],
            unb64(sth["signature"]),
        )
        return ok


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Transparent log client demo")
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    p_add = sub.add_parser("add"); p_add.add_argument("text")
    p_ent = sub.add_parser("entry"); p_ent.add_argument("index", type=int)
    sub.add_parser("sth")
    sub.add_parser("key")
    p_inc = sub.add_parser("inclusion")
    p_inc.add_argument("leaf_index", type=int)
    p_inc.add_argument("--tree-size", type=int)
    p_con = sub.add_parser("consistency")
    p_con.add_argument("old_size", type=int)
    p_con.add_argument("--new-size", type=int)
    args = parser.parse_args(argv)

    client = LogClient(args.url)
    if args.cmd == "health":
        print(json.dumps(client.health(), indent=2))
    elif args.cmd == "add":
        print(json.dumps(client.add_text(args.text), indent=2))
    elif args.cmd == "entry":
        print(json.dumps(client.entry(args.index), indent=2))
    elif args.cmd == "sth":
        print(json.dumps(client.sth(), indent=2))
    elif args.cmd == "key":
        print(json.dumps(client.public_key(), indent=2))
    elif args.cmd == "inclusion":
        resp = client.inclusion(args.leaf_index, args.tree_size)
        resp["locally_verified"] = client.verify_inclusion_response(resp)
        print(json.dumps(resp, indent=2))
    elif args.cmd == "consistency":
        resp = client.consistency(args.old_size, args.new_size)
        resp["locally_verified"] = client.verify_consistency_response(resp)
        print(json.dumps(resp, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
