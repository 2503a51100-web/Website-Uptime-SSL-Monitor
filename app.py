"""Website Uptime and SSL Monitor - Flask application.

Run with:  python app.py   then open http://127.0.0.1:5000
"""

import os
import json
import requests
from dotenv import load_dotenv

from flask import Flask,jsonify, render_template, request

import database
import monitor

load_dotenv()

AWS_LAMBDA_URL = os.getenv("AWS_LAMBDA_URL")
API_SECRET_KEY = os.getenv("API_SECRET_KEY")

app = Flask(__name__)
# Create the SQLite database and table automatically when the app starts.
database.init_db()

def call_aws(action, data=None):
    """Send an authenticated request from Flask to AWS Lambda."""
    if not AWS_LAMBDA_URL:
        raise RuntimeError("AWS_LAMBDA_URL is not configured.")

    if not API_SECRET_KEY:
        raise RuntimeError("API_SECRET_KEY is not configured.")

    payload = {"action": action}

    if data:
        payload.update(data)

    response = requests.post(
        AWS_LAMBDA_URL,
        json=payload,
        headers={
            "x-api-key": API_SECRET_KEY
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if "statusCode" in result:
        status_code = result["statusCode"]
        body = result.get("body", "{}")

        if status_code >= 400:
            raise RuntimeError(body)

        if isinstance(body, str):
            return json.loads(body)

        return body

    return result


MAX_HISTORY = 500


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/check", methods=["POST"])
def api_check():
    """Check a website using AWS Lambda and save a local copy."""
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(error="Send a JSON body like {\"url\": \"google.com\"}."), 400

    try:
        result = call_aws("check", {"url": data.get("url")})

        # Keep a local copy in SQLite for the existing History interface.
        result["id"] = database.insert_check(result)

        return jsonify(result), 201

    except requests.RequestException as exc:
        return jsonify(error=f"AWS connection failed: {str(exc)}"), 502

    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502

@app.route("/api/history", methods=["GET"])
def api_history():
    """Get monitoring history from AWS and maintain local SQLite copies."""
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, MAX_HISTORY))
    url = request.args.get("url") or None

    try:
        payload = {"limit": limit}

        if url:
            payload["url"] = url

        aws_result = call_aws("history", payload)
        records = aws_result.get("records", [])

        if not isinstance(records, list):
            raise RuntimeError("AWS returned an invalid history response.")

        for record in records:
            local_id = database.get_check_by_url_and_checked_at(
                record["url"],
                record["checked_at"]
            )

            if local_id is None:
                local_id = database.insert_check(record)

            record["id"] = local_id

        return jsonify(records), 200

    except (requests.RequestException, RuntimeError, KeyError, TypeError) as exc:
        return jsonify(error=f"Could not retrieve AWS history: {str(exc)}"), 502


@app.route("/api/history/<int:check_id>", methods=["GET"])
def api_history_item(check_id):
    record = database.get_check(check_id)
    if record is None:
        return jsonify(error="Record not found."), 404
    return jsonify(record), 200


@app.route("/api/history/<int:check_id>", methods=["DELETE"])
def api_history_delete(check_id):
    # First, find the record in local SQLite.
    record = database.get_check(check_id)

    if record is None:
        return jsonify(error="Record not found."), 404

    try:
        # Delete the matching record from AWS DynamoDB.
        aws_result = call_aws("delete", {
            "url": record["url"],
            "checked_at": record["checked_at"]
        })

        # Delete locally only after AWS confirms success.
        if (
            not isinstance(aws_result, dict)
            or aws_result.get("message") != "Record deleted successfully."
        ):
            return jsonify(error="AWS did not confirm the deletion."), 502

        database.delete_check(check_id)

        return jsonify(
            message="Record deleted.",
            id=check_id
        ), 200

    except requests.RequestException as exc:
        return jsonify(error=f"AWS connection failed: {str(exc)}"), 502

    except RuntimeError as exc:
        return jsonify(error=f"AWS deletion failed: {str(exc)}"), 502


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Get uptime statistics from AWS DynamoDB."""
    url = request.args.get("url") or None

    try:
        payload = {}

        if url:
            payload["url"] = url

        result = call_aws("stats", payload)

        return jsonify(result), 200

    except requests.RequestException as exc:
        return jsonify(error=f"AWS connection failed: {str(exc)}"), 502

    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502

@app.route("/api/sites", methods=["GET"])
def api_sites():
    """Get distinct website URLs from AWS DynamoDB."""
    try:
        result = call_aws("sites")

        if not isinstance(result, list):
            raise RuntimeError("AWS returned an invalid website list.")

        return jsonify(result), 200

    except requests.RequestException as exc:
        return jsonify(error=f"AWS connection failed: {str(exc)}"), 502

    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


# ---------------------------------------------------------------------------
# JSON errors for /api routes
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify(error="API route not found."), 404
    return error


@app.errorhandler(405)
def method_not_allowed(error):
    if request.path.startswith("/api/"):
        return jsonify(error="Method not allowed for this route."), 405
    return error


@app.errorhandler(500)
def server_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error="Internal server error."), 500
    return error


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
    )
