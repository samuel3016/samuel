"""Production runner for the CPS Claude Bridge.

The existing app.py remains the business logic/source of truth. This runner
only changes /message transport behaviour: acknowledge immediately and finish
Claude processing in the background, then deliver the completed response to
Make's callback webhook.
"""
import json
import os
import threading
import requests
from http.server import HTTPServer

import app

CALLBACK_URL = os.environ.get("MAKE_CLAUDE_CALLBACK_WEBHOOK_URL", "").strip()


def send_json(handler, status, payload):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def worker(data):
    try:
        answer = app.process(
            data.get("message", ""),
            data.get("source", "Unknown"),
            data.get("thread_id", ""),
            data.get("sender_name", ""),
            data.get("sender_email", ""),
            data.get("subject", ""),
            data.get("message_id", ""),
            data.get("received_at", ""),
        )
        payload = dict(data)
        payload.update({"status": "success", "response": answer, "callback_version": "1"})
    except Exception as exc:
        payload = dict(data)
        payload.update({"status": "error", "response": "", "error": str(exc), "callback_version": "1"})

    if not CALLBACK_URL:
        print("CPS callback not configured; response completed without callback", flush=True)
        return

    try:
        r = requests.post(CALLBACK_URL, json=payload, timeout=15)
        print(f"CPS callback delivered: HTTP {r.status_code}", flush=True)
    except Exception as exc:
        print(f"CPS callback delivery failed: {exc}", flush=True)


original_do_post = app.Handler.do_POST


def async_do_post(self):
    if self.path.split("?")[0] != "/message":
        return original_do_post(self)
    try:
        n = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(n).decode())
        if not data.get("message"):
            raise ValueError("Missing message")

        send_json(self, 202, {
            "status": "accepted",
            "source": data.get("source", "Unknown"),
            "message_id": data.get("message_id", ""),
            "thread_id": data.get("thread_id", ""),
            "callback": True,
        })
        threading.Thread(target=worker, args=(data,), daemon=True).start()
    except Exception:
        return original_do_post(self)


app.Handler.do_POST = async_do_post
print("CPS async callback runner enabled", flush=True)

if __name__ == "__main__":
    HTTPServer((app.BRIDGE_HOST, app.BRIDGE_PORT), app.Handler).serve_forever()
