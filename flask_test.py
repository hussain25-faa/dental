import os

from flask import Flask, request, redirect, session, url_for, send_from_directory

import app_old as legacy


app = Flask(__name__)

# Local/offline application secret key.
# You can later move this to an environment variable.
app.secret_key = os.environ.get(
    "DENTAL_SECRET_KEY",
    "dental-clinic-local-secret-change-later"
)

# Existing folders
app.config["UPLOAD_FOLDER"] = str(legacy.UPLOADS_DIR)


def get_session_data():
    """Convert Flask session into the format used by the old application."""
    if not session.get("logged_in"):
        return None

    return {
        "user_name": str(session.get("user_name", "Admin")),
        "user_role": str(session.get("user_role", "Admin")),
        "staff_id": str(session.get("staff_id", "")),
        "is_admin": str(session.get("is_admin", "0")),
    }


def prepare_context(session_data):
    """Prepare the old application's request context for Flask."""
    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            settings = legacy._get_settings(conn)
            legacy._apply_request_context(session_data, settings, conn)
        finally:
            conn.close()

    return settings


@app.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in
    current = get_session_data()

    if request.method == "GET":
        if current:
            settings = prepare_context(current)
            return redirect(legacy._default_landing_path(current, settings))

        return legacy.page_login()

    # ---------- POST LOGIN ----------

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        return legacy.page_login("Enter username and password.")

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            settings = legacy._get_settings(conn)

            staff = conn.execute(
                """
                SELECT *
                FROM staff
                WHERE lower(name) = lower(?)
                  AND active = 1
                """,
                (username,),
            ).fetchone()

        finally:
            conn.close()

    # Admin login
    if username.lower() == "admin":

        if password != settings.get("admin_password", "admin123"):
            return legacy.page_login("Invalid login details.")

        session_data = {
            "user_name": "Admin",
            "user_role": "Admin",
            "is_admin": "1",
        }

    # Staff login
    else:

        if not staff or (staff["password"] or "") != password:
            return legacy.page_login("Invalid login details.")

        session_data = {
            "user_name": str(staff["name"]),
            "user_role": str(staff["role"] or "Staff"),
            "staff_id": str(staff["id"]),
            "is_admin": "0",
        }

    # Store login in Flask session
    session.clear()
    session["logged_in"] = True
    session.update(session_data)

    legacy._request_ctx.user_name = session_data["user_name"]

    return redirect(
        legacy._default_landing_path(session_data, settings)
    )


@app.route("/logout")
def logout():
    session.clear()
    legacy._request_ctx.user_name = "Admin"
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "dashboard",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_dashboard()


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(
        legacy.UPLOADS_DIR,
        filename,
    )


@app.errorhandler(404)
def not_found(error):
    return legacy._layout(
        "Not Found",
        "<div class='card'>"
        "<h1>404 Not Found</h1>"
        "</div>",
    ), 404


@app.errorhandler(500)
def server_error(error):
    return legacy._layout(
        "Error",
        "<div class='card'>"
        "<h1>Error</h1>"
        "<div class='muted mono'>"
        "Internal server error"
        "</div>"
        "</div>",
    ), 500


if __name__ == "__main__":
    print("Starting Dental Clinic Flask application...")
    print("Database:", legacy.DB_PATH)
    print("Open: http://127.0.0.1:5000")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )