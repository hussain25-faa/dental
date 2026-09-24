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

@app.route("/patients")
def patients():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_patients_list(request.args)


@app.route("/patients/new")
def patient_new():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_patient_new()


@app.route("/patients/<int:patient_id>")
def patient_detail(patient_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_patient_detail(patient_id)

@app.route("/appointments")
def appointments():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "appointments",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_appointments(request.args)


@app.route("/appointments/<int:appointment_id>/done")
def appointment_done(appointment_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "appointments",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            conn.execute(
                "UPDATE appointments SET status = 'Done' WHERE id = ?",
                (appointment_id,),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(url_for("appointments"))

@app.route("/patients/<int:patient_id>/delete")
def patient_delete(patient_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    if not legacy._can_delete_records():
        return legacy._access_denied_page(), 403

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            conn.execute(
                "DELETE FROM patients WHERE id = ?",
                (patient_id,),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(url_for("patients"))

@app.route("/patients/new", methods=["POST"])
def patient_new_post():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    name = (request.form.get("name") or "").strip()
    age_raw = (request.form.get("age") or "").strip()
    gender = (request.form.get("gender") or "").strip()
    phone = (request.form.get("phone") or "").strip()

    if not name:
        return legacy.page_patient_new(
            "Patient name is required."
        )

    age = None

    if age_raw:
        try:
            age = int(age_raw)
        except ValueError:
            return legacy.page_patient_new(
                "Age must be a number."
            )

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            code = legacy._next_patient_code(conn)

            conn.execute(
                """
                INSERT INTO patients(
                    patient_code,
                    name,
                    age,
                    gender,
                    phone,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    age,
                    gender,
                    phone,
                    legacy._iso_now_minutes(),
                ),
            )

            pid = int(
                conn.execute(
                    "SELECT last_insert_rowid() AS id"
                ).fetchone()["id"]
            )

            conn.commit()

        finally:
            conn.close()

    return redirect(url_for("patient_detail", patient_id=pid))


@app.route("/appointments/new", methods=["POST"])
def appointment_new_post():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "appointments",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    patient_id = int(
        (request.form.get("patient_id") or "0").strip() or "0"
    )

    appointment_at = (
        request.form.get("appointment_at") or ""
    ).strip()

    doctor_name = (
        request.form.get("doctor_name") or ""
    ).strip()

    note = (
        request.form.get("note") or ""
    ).strip()

    status = (
        request.form.get("status") or "Scheduled"
    ).strip() or "Scheduled"

    if not patient_id or not appointment_at:
        return legacy.page_appointments(
            request.args,
            flash="Patient and appointment date are required.",
        )

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            conn.execute(
                """
                INSERT INTO appointments(
                    patient_id,
                    appointment_at,
                    doctor_name,
                    note,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    appointment_at,
                    doctor_name,
                    note,
                    status,
                    legacy._iso_now_minutes(),
                ),
            )

            conn.commit()

        finally:
            conn.close()

    source_patient_id = (
        request.form.get("source_patient_id") or ""
    ).strip()

    if source_patient_id:
        return redirect(
            url_for(
                "patient_detail",
                patient_id=int(source_patient_id),
            )
        )

    return redirect(url_for("appointments"))

@app.route("/attendance", methods=["GET"])
def attendance():
    session_data = get_session_data()
    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data, settings, "attendance"
    ):
        return redirect(
            legacy._default_landing_path(
                session_data, settings
            )
        )

    day = (request.args.get("day") or "").strip()

    return legacy.page_attendance(
        day,
        session_data
    )


@app.route("/attendance", methods=["POST"])
def attendance_post():
    session_data = get_session_data()
    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data, settings, "attendance"
    ):
        return redirect(
            legacy._default_landing_path(
                session_data, settings
            )
        )

    day = (
        request.form.get("day")
        or request.args.get("day")
        or legacy._now_local().date().isoformat()
    ).strip()

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            if (
                session_data.get("is_admin") != "1"
                and session_data.get("staff_id")
            ):
                staff = conn.execute(
                    """
                    SELECT id
                    FROM staff
                    WHERE active = 1 AND id = ?
                    """,
                    (
                        int(
                            (
                                session_data.get("staff_id")
                                or "0"
                            ).strip()
                            or "0"
                        ),
                    ),
                ).fetchall()
            else:
                staff = conn.execute(
                    """
                    SELECT id
                    FROM staff
                    WHERE active = 1
                    """
                ).fetchall()

            for s in staff:
                sid = int(s["id"])

                status = (
                    request.form.get(f"status_{sid}")
                    or "Absent"
                ).strip()

                cin = (
                    request.form.get(f"check_in_{sid}")
                    or ""
                ).strip() or None

                cout = (
                    request.form.get(f"check_out_{sid}")
                    or ""
                ).strip() or None

                conn.execute(
                    """
                    INSERT INTO attendance(
                        staff_id,
                        day,
                        status,
                        check_in,
                        check_out,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)

                    ON CONFLICT(staff_id, day)
                    DO UPDATE SET
                        status=excluded.status,
                        check_in=excluded.check_in,
                        check_out=excluded.check_out
                    """,
                    (
                        sid,
                        day,
                        status,
                        cin,
                        cout,
                        legacy._iso_now_minutes(),
                    ),
                )

            conn.commit()

        finally:
            conn.close()

    return redirect(
        url_for(
            "attendance",
            day=day
        )
    )

@app.route("/staff")
def staff():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_staff_list(request.args)


@app.route("/staff/new")
def staff_new():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_staff_new()


@app.route("/staff/<int:staff_id>")
def staff_view(staff_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_staff_view(staff_id)


@app.route("/staff/<int:staff_id>/edit")
def staff_edit(staff_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_staff_edit(staff_id)

@app.route("/staff/new", methods=["POST"])
def staff_new_post():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    address = (request.form.get("address") or "").strip()
    aadhar_no = (request.form.get("aadhar_no") or "").strip()
    role = (request.form.get("role") or "").strip() or "Staff"

    if not name:
        return legacy.page_staff_new(
            "Staff name is required."
        )

    staff_image = request.files.get("staff_image")
    aadhar_image = request.files.get("aadhar_image")

    staff_image_path = ""
    aadhar_image_path = ""

    if staff_image and staff_image.filename:
        staff_image_path = legacy._save_uploaded_file(
            staff_image,
            "staff_photo",
        )

    if aadhar_image and aadhar_image.filename:
        aadhar_image_path = legacy._save_uploaded_file(
            aadhar_image,
            "aadhar",
        )

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            conn.execute(
                """
                INSERT INTO staff(
                    name,
                    phone,
                    address,
                    aadhar_no,
                    staff_image_path,
                    aadhar_image_path,
                    role,
                    password,
                    monthly_salary_paise,
                    active,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    name,
                    phone,
                    address,
                    aadhar_no,
                    staff_image_path,
                    aadhar_image_path,
                    role,
                    "",
                    0,
                    legacy._iso_now_minutes(),
                ),
            )

            conn.commit()

        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                return legacy.page_staff_new(
                    "Staff name already exists."
                )
            raise

        finally:
            conn.close()

    return redirect(url_for("staff"))

@app.route("/staff/<int:staff_id>/edit", methods=["POST"])
def staff_edit_post(staff_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "staff",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    address = (request.form.get("address") or "").strip()
    aadhar_no = (request.form.get("aadhar_no") or "").strip()
    role = (request.form.get("role") or "").strip() or "Staff"

    active = (
        1
        if (request.form.get("active") or "1").strip() == "1"
        else 0
    )

    if not name:
        return legacy.page_staff_edit(
            staff_id,
            error="Staff name is required.",
        )

    staff_image = request.files.get("staff_image")
    aadhar_image = request.files.get("aadhar_image")

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            current = conn.execute(
                """
                SELECT staff_image_path, aadhar_image_path
                FROM staff
                WHERE id = ?
                """,
                (staff_id,),
            ).fetchone()

            staff_image_path = (
                legacy._save_uploaded_file(
                    staff_image,
                    "staff_photo",
                )
                if staff_image and staff_image.filename
                else (
                    current["staff_image_path"]
                    if current
                    else ""
                )
            )

            aadhar_image_path = (
                legacy._save_uploaded_file(
                    aadhar_image,
                    "aadhar",
                )
                if aadhar_image and aadhar_image.filename
                else (
                    current["aadhar_image_path"]
                    if current
                    else ""
                )
            )

            conn.execute(
                """
                UPDATE staff
                SET
                    name = ?,
                    phone = ?,
                    address = ?,
                    aadhar_no = ?,
                    staff_image_path = ?,
                    aadhar_image_path = ?,
                    role = ?,
                    active = ?
                WHERE id = ?
                """,
                (
                    name,
                    phone,
                    address,
                    aadhar_no,
                    staff_image_path,
                    aadhar_image_path,
                    role,
                    active,
                    staff_id,
                ),
            )

            conn.commit()

        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                return legacy.page_staff_edit(
                    staff_id,
                    error="Staff name already exists.",
                )
            raise

        finally:
            conn.close()

    return redirect(url_for("staff"))

@app.route("/expenses")
def expenses():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (request.args.get("day") or "").strip()

    return legacy.page_expenses(day)

@app.route("/staff-adjustments/new", methods=["POST"])
def staff_adjustment_new():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (
        request.args.get("day")
        or legacy._now_local().date().isoformat()
    ).strip()

    staff_id = int(
        (request.form.get("staff_id") or "0").strip() or "0"
    )

    kind = (
        request.form.get("kind") or "Expense"
    ).strip()

    amount_raw = (
        request.form.get("amount") or ""
    ).strip()

    note = (
        request.form.get("note") or ""
    ).strip()

    try:
        amount_paise = legacy._money_to_paise(amount_raw)
    except ValueError:
        return legacy.page_staff_adjustments(
            day,
            flash="Invalid amount value.",
        )

    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            conn.execute(
                """
                INSERT INTO staff_adjustments(
                    staff_id,
                    day,
                    kind,
                    amount_paise,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    staff_id,
                    day,
                    kind,
                    amount_paise,
                    note,
                    legacy._iso_now_minutes(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(
        url_for("expenses", day=day)
    )

@app.route("/staff-adjustments")
def staff_adjustments():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (request.args.get("day") or "").strip()

    return legacy.page_staff_adjustments(day)

@app.route("/staff-adjustments/<int:adj_id>/delete")
def staff_adjustment_delete(adj_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (
        request.args.get("day")
        or legacy._now_local().date().isoformat()
    ).strip()

    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            conn.execute(
                "DELETE FROM staff_adjustments WHERE id = ?",
                (adj_id,),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(
        url_for("staff_adjustments", day=day)
    )
@app.route("/clinic-expenses/<int:exp_id>/delete")
def clinic_expense_delete(exp_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (
        request.args.get("day")
        or legacy._now_local().date().isoformat()
    ).strip()

    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            conn.execute(
                "DELETE FROM clinic_expenses WHERE id = ?",
                (exp_id,),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(
        url_for("expenses", day=day)
    )



@app.route("/clinic-expenses/new", methods=["POST"])
def clinic_expense_new():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "expenses",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    day = (
        request.args.get("day")
        or legacy._now_local().date().isoformat()
    ).strip()

    category = (
        request.form.get("category") or "Other"
    ).strip()

    amount_raw = (
        request.form.get("amount") or ""
    ).strip()

    note = (
        request.form.get("note") or ""
    ).strip()

    try:
        amount_paise = legacy._money_to_paise(amount_raw)
    except ValueError:
        return legacy.page_clinic_expenses(
            day,
            flash="Invalid amount value.",
        )

    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            conn.execute(
                """
                INSERT INTO clinic_expenses(
                    day,
                    category,
                    amount_paise,
                    note,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    day,
                    category,
                    amount_paise,
                    note,
                    legacy._iso_now_minutes(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(
        url_for("expenses", day=day)
    )

@app.route(
    "/patients/<int:patient_id>/visits/new",
    methods=["POST"],
)
def patient_visit_new(patient_id):
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "patients",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    visit_at = (
        request.form.get("visit_at") or ""
    ).strip()

    doctor = (
        request.form.get("doctor_assigned") or ""
    ).strip()

    treatment = (
        request.form.get("treatment_details") or ""
    ).strip()

    notes = (
        request.form.get("notes") or ""
    ).strip()

    cost_raw = (
        request.form.get("cost") or ""
    ).strip()

    if not visit_at:
        return redirect(
            url_for(
                "patient_detail",
                patient_id=patient_id,
            )
        )

    try:
        cost_paise = legacy._money_to_paise(
            cost_raw
        )
    except ValueError:
        return legacy.page_patient_detail(
            patient_id,
            flash=(
                "Invalid cost value. "
                "Use numbers like 500 or 500.00"
            ),
        )

    with legacy._db_lock:
        conn = legacy.db_connect()
        try:
            conn.execute(
                """
                INSERT INTO visits(
                    patient_id,
                    visit_at,
                    doctor_assigned,
                    treatment_details,
                    notes,
                    cost_paise,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    visit_at,
                    doctor,
                    treatment,
                    notes,
                    cost_paise,
                    legacy._iso_now_minutes(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return redirect(
        url_for(
            "patient_detail",
            patient_id=patient_id,
        )
    )


@app.route("/settings")
def settings_page():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "settings",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    return legacy.page_settings()

@app.route("/settings", methods=["POST"])
def settings_save():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "settings",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    clinic_name = (
        request.form.get("clinic_name") or ""
    ).strip() or "Dental Clinic"

    clinic_tagline = (
        request.form.get("clinic_tagline") or ""
    ).strip() or "Management System"

    admin_password = (
        request.form.get("admin_password") or ""
    ).strip() or "admin123"

    theme = (
        request.form.get("theme") or ""
    ).strip() or "ocean"

    clinic_phone = (
        request.form.get("clinic_phone") or ""
    ).strip()

    clinic_email = (
        request.form.get("clinic_email") or ""
    ).strip()

    clinic_address = (
        request.form.get("clinic_address") or ""
    ).strip()

    currency_symbol = (
        request.form.get("currency_symbol") or ""
    ).strip() or "Rs."

    appointment_minutes = (
        request.form.get("appointment_minutes") or ""
    ).strip() or "30"

    with legacy._db_lock:
        conn = legacy.db_connect()

        try:
            role_names = legacy._roles_from_staff(
                conn,
                active_only=True,
            )

            role_permissions = {}

            for role in role_names:
                allowed = [
                    module
                    for module in legacy.MODULES
                    if (
                        request.form.get(
                            f"perm__{role}__{module}"
                        ) or ""
                    ).strip() == "1"
                ]

                role_permissions[role] = (
                    allowed
                    or list(
                        legacy.DEFAULT_ROLE_PERMISSIONS.get(
                            role,
                            ["dashboard"],
                        )
                    )
                )

            settings_values = {
                "clinic_name": clinic_name,
                "clinic_tagline": clinic_tagline,
                "admin_password": admin_password,
                "theme": theme,
                "role_options": ",".join(role_names),
                "clinic_phone": clinic_phone,
                "clinic_email": clinic_email,
                "clinic_address": clinic_address,
                "currency_symbol": currency_symbol,
                "appointment_minutes": appointment_minutes,
                "role_permissions": legacy.json.dumps(
                    role_permissions
                ),
            }

            for key, value in settings_values.items():
                conn.execute(
                    """
                    INSERT INTO app_settings(key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key)
                    DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

            staff_rows = conn.execute(
                "SELECT id FROM staff"
            ).fetchall()

            for row in staff_rows:
                sid = int(row["id"])

                staff_role = (
                    request.form.get(
                        f"staff_role_{sid}"
                    ) or ""
                ).strip() or "Staff"

                if staff_role not in role_names:
                    staff_role = "Staff"

                staff_password = (
                    request.form.get(
                        f"staff_password_{sid}"
                    ) or ""
                ).strip()

                conn.execute(
                    """
                    UPDATE staff
                    SET role = ?, password = ?
                    WHERE id = ?
                    """,
                    (
                        staff_role,
                        staff_password,
                        sid,
                    ),
                )

            conn.commit()

        finally:
            conn.close()

    legacy._request_ctx.clinic_name = clinic_name
    legacy._request_ctx.clinic_tagline = clinic_tagline
    legacy._request_ctx.theme = theme

    return redirect(url_for("settings_page"))

@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(
        legacy.UPLOADS_DIR,
        filename,
    )

@app.route("/reports")
def reports():
    session_data = get_session_data()

    if not session_data:
        return redirect(url_for("login"))

    settings = prepare_context(session_data)

    if not legacy._has_module_access(
        session_data,
        settings,
        "reports",
    ):
        return redirect(
            legacy._default_landing_path(
                session_data,
                settings,
            )
        )

    query = request.args.to_dict()

    return legacy.page_reports(query)

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