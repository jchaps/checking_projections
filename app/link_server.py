"""Minimal local HTTP server to run the Plaid Link flow in a browser."""

import json
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8484
_result = {"public_token": None}
_server_ref = {"server": None}

LINK_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Plaid Link</title>
  <style>
    body {
      font-family: -apple-system, Helvetica, Arial, sans-serif;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh; margin: 0; background: #f7f9fc; color: #333;
    }
    .container { text-align: center; max-width: 500px; }
    h2 { color: #2c3e50; }
    p { color: #666; line-height: 1.6; }
    .status { margin-top: 20px; font-weight: bold; }
    .success { color: #27ae60; }
    .error { color: #c0392b; }
  </style>
</head>
<body>
  <div class="container">
    <h2>Checking Projections</h2>
    <p id="status">Initializing Plaid Link...</p>
  </div>

  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    const handler = Plaid.create({
      token: '{{LINK_TOKEN}}',
      onSuccess: function(public_token, metadata) {
        document.getElementById('status').innerHTML =
          '<span class="success">Connected! You can close this tab.</span>';
        fetch('/callback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({public_token: public_token})
        });
      },
      onExit: function(err, metadata) {
        if (err) {
          document.getElementById('status').innerHTML =
            '<span class="error">Link failed: ' + (err.display_message || err.error_code) + '</span>';
        } else {
          document.getElementById('status').innerHTML =
            '<span class="error">Link was closed. You can close this tab.</span>';
        }
        fetch('/callback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({public_token: null})
        });
      },
      onLoad: function() {
        handler.open();
      }
    });
  </script>
</body>
</html>"""


class LinkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            html = LINK_HTML.replace("{{LINK_TOKEN}}", self.server.link_token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/callback":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            _result["public_token"] = body.get("public_token")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            # Shut down server after receiving callback
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


def run_link_flow(link_token):
    """Start a local server, open the browser, and wait for the Plaid Link callback."""
    _result["public_token"] = None

    server = HTTPServer(("127.0.0.1", PORT), LinkHandler)
    server.link_token = link_token

    url = f"http://localhost:{PORT}"
    print(f"Opening Plaid Link in your browser at {url}")
    print("Complete the login flow in the browser. Waiting...\n")
    webbrowser.open(url)

    server.serve_forever()
    server.server_close()

    return _result["public_token"]
