# ABS Dental — DEMO ONLY

This is a separate portfolio demo build.

Run:
    python demo_app.py

Open:
    http://127.0.0.1:9000/demo

There is NO Admin login in this build.
`/login` redirects to `/demo`.
`/demo` automatically creates a Demo Visitor session.

Demo can VIEW:
- Dashboard
- Patients
- Appointments
- Attendance
- Staff
- Expenses
- Reports

Demo cannot access:
- Settings
- Role & Access
- Admin controls

All POST/write requests are blocked while in Demo mode.
The demo uses `demo.sqlite3`, which is created and seeded automatically with fictional data.
It does NOT use `clinic.sqlite3`.

Do not copy the real clinic.sqlite3 into this demo folder.

Install:
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python demo_app.py
