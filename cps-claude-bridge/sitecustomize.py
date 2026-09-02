"""CPS Claude Bridge async handoff shim.

Loaded automatically by Python before app.py. It intercepts the HTTPServer
handler used by the existing bridge and changes only /message requests:
respond immediately, then process Claude in a background thread and POST the
completed response to Make.

If MAKE_CLAUDE_CALLBACK_WEBHOOK_URL is absent, the original handler is used.
"""
import json
import os
import threading
import requests
from http.server import HTTPServer

_CALLBACK_URL = os.environ.get("MAKE_CLAUDE_CALLBACK_WEBHOOK_URL", "").strip()
_PATCHED = False


def _install_async_handler():
    global _PATCHED
    if _PATCHED or not _CALLBACK_URL:
        return

    original_serve_forever = HTTPServer.serve_forever

    def serve_forever_with_async_handler(server, *args, **kwargs):
        handler_cls = server.RequestHandlerClass
        original_do_post = handler_cls.do_POST

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def worker(data):
            try:
                import app
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
                payload.update({
                    "status": "success",
                    "response": answer,
                    "callback_version": "1",
                })
            except Exception as exc:
                payload = dict(data)
                payload.update({
                    "status": "error",
                    "response": "",
                    "error": str(exc),
                    "callback_version": "1",
                })

            try:
                requests.post(_CALLBACK_URL, json=payload, timeout=15)
            except Exception as exc:
                print(f"CPS callback delivery failed: {exc}", flush=True)

        def async_do_post(self):
            if self.path.split("?")[0] != "/message":
                return original_do_post(self)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n).decode()
                data = json.loads(raw)
                if not data.get("message"):
                    raise ValueError("Missing message")

                # Acknowledge Make/ManyChat immediately. Claude processing is
                # deliberately detached from this HTTP request.
                send_json(self, 202, {
                    "status": "accepted",
                    "message_id": data.get("message_id", ""),
                    "thread_id": data.get("thread_id", ""),
                    "callback": True,
                })

                threading.Thread(target=worker, args=(data,), daemon=True).start()
            except Exception:
                return original_do_post(self)

        handler_cls.do_POST = async_do_post
        print("CPS async callback handoff enabled", flush=True)
        return original_serve_forever(server, *args, **kwargs)

    HTTPServer.serve_forever = serve_forever_with_async_handler
    _PATCHED = True


_install_async_handler()
