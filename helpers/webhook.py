#!/usr/bin/env python3
"""
Listens for incoming HTTP requests and prints the JSON payload to the console.

Usage:
    pip install flask
    python webhook.py

Then send a test request:
    curl -X POST http://localhost:5000/webhook \
         -H "Content-Type: application/json" \
         -d '{"event": "test", "value": 42}'
"""

import json
from flask import Flask, request, jsonify

app = Flask(__name__)


@app.route("/", methods=["POST"])
def webhook():
    payload = request.get_json(force=True, silent=True)
    print("\n--- Webhook received ---")
    if payload is not None:
        print(json.dumps(payload, indent=2))
    else:
        print("(could not parse JSON, raw body below)")
        print(request.get_data(as_text=True))

    print("------------------------\n")
    return jsonify({"status": "received"}), 200


@app.route("/", methods=["GET"])
def health():
    return "Webhook receiver is running. POST to /webhook", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)