from functools import wraps
from flask import request, jsonify
from .database import Database

_db: Database = None


def init_auth(db: Database):
    global _db
    _db = db


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not api_key:
            api_key = request.args.get("api_key", "")

        if not api_key:
            return jsonify({"error": "missing api key"}), 401

        node = _db.get_node_by_api_key(api_key)
        if not node:
            return jsonify({"error": "invalid api key"}), 403

        kwargs["_node"] = node
        return f(*args, **kwargs)

    return decorated
