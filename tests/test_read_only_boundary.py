import ast
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch
import httpx
from tests.support import *

class ReadOnlyBoundaryTests(unittest.TestCase):
    def test_offline_suite_blocks_network(self):
        import socket
        with socket.socket() as connection:
            connection.settimeout(0.05)
            with self.assertRaisesRegex(AssertionError,"Offline tests"):
                connection.connect(("127.0.0.1",9))

    def test_request_surface_is_public_get_only(self):
        from polymarket_bot.cli import main
        for forced in (False,True):
            with self.subTest(forced=forced):
                requests = []
                def handle(r):
                    requests.append(r)
                    if r.url.path == "/time": value = int(NOW.timestamp())
                    elif r.url.path == "/events": value = [make_event()]
                    elif r.url.path.startswith("/events/slug/"): value = make_event()
                    elif r.url.path == "/markets/1001": value = make_market()
                    elif r.url.path == "/book":
                        token = r.url.params["token_id"]
                        value = make_book(asset_id=token)
                    else: raise AssertionError(f"Forbidden request {r.url}")
                    return httpx.Response(200,stream=httpx.ByteStream(json.dumps(value).encode()))
                client = httpx.Client(transport=httpx.MockTransport(handle))
                out,err = io.StringIO(),io.StringIO()
                clock = FakeClock()
                with patch("polymarket_bot.cli.httpx.Client",return_value=client) as create, patch("sys.stdout",out), patch("sys.stderr",err), patch("polymarket_bot.cli.time.monotonic",clock.monotonic):
                    self.assertEqual(main(["--no-fast-path"] if forced else []),0)
                self.assertEqual(create.call_args.kwargs,{"trust_env":False,"follow_redirects":False})
                self.assertTrue(client.is_closed)
                self.assertIn("Discovery: filtered_list" if forced else "Discovery: fast_path",out.getvalue())
                self.assertEqual([r.url.params["token_id"] for r in requests if r.url.path=="/book"],[TOKEN_UP,TOKEN_DOWN])
                self.assertTrue(all(r.method == "GET" for r in requests))
                for r in requests:
                    self.assertIn(r.url.host,("gamma-api.polymarket.com","clob.polymarket.com"))
                    self.assertTrue(r.url.path in ("/time","/book","/events","/markets/1001") or r.url.path.startswith("/events/slug/"))
                    self.assertFalse(any("authorization" in h.lower() or "api-key" in h.lower() or "signature" in h.lower() for h in r.headers))

    def test_wrong_identity_book_is_not_accepted_in_full_run(self):
        from polymarket_bot.cli import run_snapshot
        clock = FakeClock()
        clob = FakeClob(clock)
        clob.books[TOKEN_UP] = make_book(asset_id=TOKEN_DOWN)
        out,err = io.StringIO(),io.StringIO()
        self.assertEqual(run_snapshot(FakeGamma(),clob,monotonic=clock.monotonic,use_fast_path=True,jakarta=False,debug=False,stdout=out,stderr=err),1)
        self.assertEqual(out.getvalue(),"")
        self.assertIn("book_identity_mismatch",err.getvalue())

    def test_application_has_only_named_public_read_request_surface(self):
        root = Path(__file__).resolve().parents[1]/"src"
        raw_sites = []
        forbidden = []
        chainlink_sends = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import,ast.ImportFrom)):
                    names = [alias.name for alias in node.names] if isinstance(node,ast.Import) else [node.module or ""]
                    if any(re.search(r"(py_clob_client|web3|eth_account|requests|urllib.request)",n) for n in names):
                        forbidden.append((path.name,node.lineno))
                if isinstance(node,ast.Constant) and isinstance(node.value,str):
                    if re.search(r"^https?://",node.value):
                        if node.value == "https://data.chain.link/streams/btc-usd-twap-60s-streams":
                            # Inert feed provenance; executable request sites remain checked below.
                            self.assertEqual(path, root/"polymarket_bot"/"recorder"/"store.py")
                        elif re.search(r"^https?://[^/]*dataengine\.chain\.link", node.value):
                            self.assertIn(path.name, ("chainlink_discovery.py", "chainlink_rest.py", "chainlink_ws.py"))
                        else:
                            self.assertIn(node.value,("https://gamma-api.polymarket.com","https://clob.polymarket.com",
                                                      "https://data-api.polymarket.com", "https://clob.polymarket.com/books"))
                    if re.search(r"^/(order|orders|cancel|auth|api-key|balance|trade)(/|$)",node.value):
                        forbidden.append((path.name,node.lineno))
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                    if node.func.attr == "send" and path.name == "chainlink_discovery.py":
                        self.assertEqual(ast.unparse(node.func.value), "client")
                        self.assertEqual(ast.unparse(node.args[0]), "request")
                        self.assertEqual({kw.arg: ast.literal_eval(kw.value) for kw in node.keywords},
                                          {"stream": True, "follow_redirects": False})
                    if node.func.attr == "send" and path.name != "chainlink_discovery.py":
                        self.assertEqual(path.name, "chainlink_rest.py")
                        self.assertEqual(ast.unparse(node.func.value), "self._client")
                        self.assertEqual(ast.unparse(node.args[0]), "request")
                        self.assertEqual({kw.arg: ast.literal_eval(kw.value) for kw in node.keywords},
                                         {"stream": True, "follow_redirects": False})
                        chainlink_sends.append(node)
                    if node.func.attr in ("stream","request","post","put","patch","delete"):
                        raw_sites.append((path.name,node))
        self.assertEqual(len(chainlink_sends), 1)
        self.assertEqual(forbidden,[])
        self.assertEqual(len(raw_sites),1)
        name,call = raw_sites[0]
        self.assertEqual(name,"http.py")
        self.assertIsInstance(call.args[0],ast.Name)
        self.assertEqual(call.args[0].id,"method")

    def test_recorder_post_is_only_fixed_clob_books_read(self):
        from polymarket_bot.market_data.http import PublicGet
        from polymarket_bot.market_data.models import MarketDataError
        clock = FakeClock()
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200,stream=httpx.ByteStream(b"[]"))
        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
            reader = PublicGet(client, monotonic=clock.monotonic, sleep=clock.sleep,
                               utcnow=clock.utcnow, max_attempts=2)
            with self.assertRaises(MarketDataError):
                reader.get_json("clob", "/books", deadline=2)
            with self.assertRaises(MarketDataError):
                reader.get_json("clob", "/orders", deadline=2)
            self.assertEqual(requests, [])
            reader.get_books((TOKEN_UP,TOKEN_DOWN), deadline=2)
        self.assertEqual(len(requests), 1)
        self.assertEqual((requests[0].method,requests[0].url.host,requests[0].url.path),
                         ("POST","clob.polymarket.com","/books"))
