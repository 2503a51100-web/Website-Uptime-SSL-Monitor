"""Basic automated tests. Run from the project folder with:

    python -m unittest discover -s tests -v

Uses only the standard library, a temporary database, and tiny local web
servers, so it needs no internet connection.
"""
import http.server
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["MONITOR_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import app as app_module  # noqa: E402
import monitor  # noqa: E402


class OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.up_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_url_normalisation(self):
        self.assertEqual(monitor.normalize_url("example.com"), "https://example.com")
        self.assertEqual(monitor.normalize_url(" http://example.com/a "), "http://example.com/a")

    def test_invalid_urls_rejected(self):
        for bad in ["", "not a url", "ftp://example.com", "hello", "http://"]:
            r = self.client.post("/api/check", json={"url": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_http_site_is_up_and_ssl_not_applicable(self):
        r = self.client.post("/api/check", json={"url": self.up_url})
        d = r.get_json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual((d["status"], d["status_code"], d["ssl_status"]), ("UP", 200, "Not Applicable"))
        self.assertGreater(d["response_time_ms"], 0)

    def test_refused_connection_is_down(self):
        d = self.client.post("/api/check", json={"url": f"http://127.0.0.1:{free_port()}"}).get_json()
        self.assertEqual(d["status"], "DOWN")
        self.assertIsNone(d["status_code"])
        self.assertIn("refused", d["error_message"].lower())

    def test_unknown_domain_is_down(self):
        d = self.client.post("/api/check", json={"url": "no-such-site-xyz123.invalid"}).get_json()
        self.assertEqual(d["status"], "DOWN")
        self.assertIn("DNS", d["error_message"])

    def test_timeout_is_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)  # accepts connections (kernel backlog) but never answers
        try:
            with mock.patch.object(monitor, "READ_TIMEOUT", 1.0):
                d = self.client.post("/api/check", json={"url": f"http://127.0.0.1:{s.getsockname()[1]}"}).get_json()
        finally:
            s.close()
        self.assertEqual(d["status"], "DOWN")
        self.assertIn("did not respond", d["error_message"])

    def test_history_stats_delete(self):
        first = self.client.post("/api/check", json={"url": self.up_url}).get_json()
        history = self.client.get("/api/history").get_json()
        self.assertTrue(any(h["id"] == first["id"] for h in history))
        stats = self.client.get("/api/stats").get_json()
        up = sum(1 for h in history if h["status"] == "UP")
        self.assertEqual(stats["total_checks"], len(history))
        self.assertEqual(stats["successful_checks"], up)
        self.assertEqual(stats["uptime_percentage"], round(up / len(history) * 100, 2))
        self.assertEqual(self.client.get(f"/api/history/{first['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/history/{first['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/history/{first['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/history/{first['id']}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
