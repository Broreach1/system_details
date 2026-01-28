#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime
from pathlib import Path
import csv
import io

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file

# -----------------------------
# Config
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent

# ✅ For Render: set env DB_PATH=/var/data/system_details.db + attach disk at /var/data
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "system_details.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_EXCHANGE_RATE = int(os.getenv("EXCHANGE_RATE", "4100"))

KHR_DENOMS = [100, 200, 500, 1000, 5000, 10000, 20000, 30000, 50000, 100000]
USD_DENOMS = [1, 5, 10, 20, 50, 100]

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()

# Telegram message max ~4096, keep buffer
MAX_TELEGRAM_LEN = 3900

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")


# -----------------------------
# DB
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            staff TEXT,
            shift TEXT,
            exchange_rate INTEGER NOT NULL,

            visa_usd REAL NOT NULL DEFAULT 0,
            aba_khr INTEGER NOT NULL DEFAULT 0,
            ac_usd REAL NOT NULL DEFAULT 0,
            ac_khr INTEGER NOT NULL DEFAULT 0,

            pos_usd REAL NOT NULL DEFAULT 0,

            expense_usd REAL NOT NULL DEFAULT 0,
            expense_khr INTEGER NOT NULL DEFAULT 0,

            cash_usd REAL NOT NULL,
            cash_khr INTEGER NOT NULL,

            total_khr INTEGER NOT NULL,
            total_usd REAL NOT NULL,

            note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_denoms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            denom INTEGER NOT NULL,
            qty INTEGER NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(report_id) REFERENCES reports(id)
        )
    """)
    conn.commit()
    conn.close()


def ensure_columns():
    """Safe migration for existing DB."""
    conn = db()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(reports)").fetchall()]

    def add_col(sql):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass

    if "expense_usd" not in cols:
        add_col("ALTER TABLE reports ADD COLUMN expense_usd REAL NOT NULL DEFAULT 0")
    if "expense_khr" not in cols:
        add_col("ALTER TABLE reports ADD COLUMN expense_khr INTEGER NOT NULL DEFAULT 0")

    conn.close()


init_db()
ensure_columns()


# -----------------------------
# Helpers
# -----------------------------
def to_int(v, default=0):
    try:
        return int(str(v).strip() or default)
    except:
        return default


def to_float(v, default=0.0):
    try:
        return float(str(v).strip() or default)
    except:
        return default


def _clean_chat_id(chat_id: str):
    """
    Telegram chat_id can be:
    - int (123, -100xxx)
    - @channelusername
    We'll return int if it's numeric, else string.
    """
    s = (chat_id or "").strip()
    if not s:
        return ""
    # numeric?
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except:
            return s
    return s


def _safe_telegram_text(text: str) -> str:
    t = (text or "").strip()
    if len(t) <= MAX_TELEGRAM_LEN:
        return t
    return t[:MAX_TELEGRAM_LEN] + "\n...(កាត់ខ្លី)"


def send_telegram_message(text: str) -> bool:
    """Send plain text to Telegram. Logs errors to Render logs."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram missing env:", {
            "has_token": bool(TELEGRAM_BOT_TOKEN),
            "has_chat_id": bool(TELEGRAM_CHAT_ID)
        })
        return False

    chat_id = _clean_chat_id(TELEGRAM_CHAT_ID)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": _safe_telegram_text(text),
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            # ✅ show real reason in Render logs
            print("Telegram send failed:", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False


def calc_totals(payload: dict):
    rate = to_int(payload.get("exchange_rate"), DEFAULT_EXCHANGE_RATE)

    khr_qty = payload.get("khr_qty", {}) or {}
    usd_qty = payload.get("usd_qty", {}) or {}

    cash_khr = 0
    cash_usd = 0.0

    khr_rows = []
    for d in KHR_DENOMS:
        qty = to_int(khr_qty.get(str(d), khr_qty.get(d, 0)), 0)
        subtotal = d * qty
        cash_khr += subtotal
        khr_rows.append({"denom": d, "qty": qty, "subtotal": subtotal})

    usd_rows = []
    for d in USD_DENOMS:
        qty = to_int(usd_qty.get(str(d), usd_qty.get(d, 0)), 0)
        subtotal = float(d) * qty
        cash_usd += subtotal
        usd_rows.append({"denom": d, "qty": qty, "subtotal": subtotal})

    visa_usd = to_float(payload.get("visa_usd"), 0.0)
    aba_khr = to_int(payload.get("aba_khr"), 0)
    ac_usd = to_float(payload.get("ac_usd"), 0.0)
    ac_khr = to_int(payload.get("ac_khr"), 0)
    pos_usd = to_float(payload.get("pos_usd"), 0.0)

    expense_usd = to_float(payload.get("expense_usd"), 0.0)
    expense_khr = to_int(payload.get("expense_khr"), 0)

    # ✅ Net total (deduct expense)
    total_khr = (
        cash_khr
        + int(round((cash_usd + visa_usd + ac_usd) * rate))
        + aba_khr
        + ac_khr
        - int(round(expense_usd * rate))
        - expense_khr
    )

    total_usd = (total_khr / rate) if rate else 0.0
    diff_usd = total_usd - pos_usd

    return {
        "exchange_rate": rate,
        "cash_khr": cash_khr,
        "cash_usd": round(cash_usd, 2),
        "visa_usd": visa_usd,
        "aba_khr": aba_khr,
        "ac_usd": ac_usd,
        "ac_khr": ac_khr,
        "pos_usd": pos_usd,
        "expense_usd": expense_usd,
        "expense_khr": expense_khr,
        "total_khr": int(total_khr),
        "total_usd": round(total_usd, 4),
        "diff_usd": round(diff_usd, 4),
        "khr_rows": khr_rows,
        "usd_rows": usd_rows,
    }


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def index():
    return render_template(
        "system_details.html",
        khr_denoms=KHR_DENOMS,
        usd_denoms=USD_DENOMS,
        default_rate=DEFAULT_EXCHANGE_RATE,
    )


@app.post("/api/calc")
def api_calc():
    payload = request.get_json(force=True) or {}
    return jsonify(calc_totals(payload))


@app.get("/test_telegram")
def test_telegram():
    ok = send_telegram_message("✅ Telegram test from System Details (Render)")
    return ("OK" if ok else "FAIL"), (200 if ok else 500)


@app.post("/save")
def save():
    staff = request.form.get("staff", "").strip()
    shift = request.form.get("shift", "").strip()
    note = (request.form.get("note", "") or "").strip()

    payload = {
        "exchange_rate": request.form.get("exchange_rate"),
        "visa_usd": request.form.get("visa_usd"),
        "aba_khr": request.form.get("aba_khr"),
        "ac_usd": request.form.get("ac_usd"),
        "ac_khr": request.form.get("ac_khr"),
        "pos_usd": request.form.get("pos_usd"),
        "expense_usd": request.form.get("expense_usd"),
        "expense_khr": request.form.get("expense_khr"),
        "khr_qty": {str(d): request.form.get(f"khr_qty_{d}", "0") for d in KHR_DENOMS},
        "usd_qty": {str(d): request.form.get(f"usd_qty_{d}", "0") for d in USD_DENOMS},
    }

    result = calc_totals(payload)

    conn = db()
    cur = conn.cursor()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
        INSERT INTO reports (
            created_at, staff, shift, exchange_rate,
            visa_usd, aba_khr, ac_usd, ac_khr,
            pos_usd,
            expense_usd, expense_khr,
            cash_usd, cash_khr,
            total_khr, total_usd,
            note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        created_at,
        staff, shift, result["exchange_rate"],
        result["visa_usd"], result["aba_khr"], result["ac_usd"], result["ac_khr"],
        result["pos_usd"],
        result["expense_usd"], result["expense_khr"],
        result["cash_usd"], result["cash_khr"],
        result["total_khr"], result["total_usd"],
        note
    ))
    report_id = cur.lastrowid

    # Save denom rows
    for row in result["khr_rows"]:
        cur.execute("""
            INSERT INTO report_denoms (report_id, currency, denom, qty, subtotal)
            VALUES (?, 'KHR', ?, ?, ?)
        """, (report_id, row["denom"], row["qty"], row["subtotal"]))

    for row in result["usd_rows"]:
        cur.execute("""
            INSERT INTO report_denoms (report_id, currency, denom, qty, subtotal)
            VALUES (?, 'USD', ?, ?, ?)
        """, (report_id, row["denom"], row["qty"], row["subtotal"]))

    conn.commit()
    conn.close()

    # ✅ Telegram (Khmer + denom breakdown)
    send_flag = request.form.get("send_telegram", "0") == "1"
    if send_flag:
        khr_lines = []
        for r in result["khr_rows"]:
            if r["qty"] > 0:
                khr_lines.append(f"៛{r['denom']:,} x {r['qty']} = ៛{int(r['subtotal']):,}")

        usd_lines = []
        for r in result["usd_rows"]:
            if r["qty"] > 0:
                usd_lines.append(f"${r['denom']} x {r['qty']} = ${float(r['subtotal']):.2f}")

        khr_block = "\n".join(khr_lines) if khr_lines else "មិនមាន"
        usd_block = "\n".join(usd_lines) if usd_lines else "មិនមាន"

        # Note can be long -> cut to safe size
        safe_note = note
        if len(safe_note) > 1200:
            safe_note = safe_note[:1200] + " ...(កាត់ខ្លី)"

        msg = (
            "📌 របាយការណ៍ព័ត៌មានលម្អិត\n"
            f"🆔 Report ID: #{report_id}\n"
            f"🕒 ម៉ោង: {created_at}\n"
            f"👤 បុគ្គលិក: {staff or '-'}\n"
            f"🧾 វេន: {shift or '-'}\n"
            f"💱 អត្រាប្ដូរប្រាក់: {result['exchange_rate']:,} រៀល/1$\n\n"
            "💰 ចំនួនប្រាក់ (រៀល):\n"
            f"{khr_block}\n\n"
            "💵 ចំនួនប្រាក់ (ដុល្លារ):\n"
            f"{usd_block}\n\n"
            f"💵 ប្រាក់សាច់ (USD): ${result['cash_usd']:.2f}\n"
            f"💰 ប្រាក់សាច់ (KHR): ៛{result['cash_khr']:,}\n"
            f"💳 វីសា (USD): ${result['visa_usd']:.2f}\n"
            f"🏦 ABA (KHR): ៛{result['aba_khr']:,}\n"
            f"💼 AC (USD): ${result['ac_usd']:.2f}\n"
            f"💼 AC (KHR): ៛{result['ac_khr']:,}\n"
            f"🧾 ចំណាយ (USD): ${result['expense_usd']:.2f}\n"
            f"🧾 ចំណាយ (KHR): ៛{result['expense_khr']:,}\n\n"
            f"✅ សរុបសុទ្ធ (KHR): ៛{result['total_khr']:,}\n"
            f"✅ សរុបសុទ្ធ (USD): ${result['total_usd']:.4f}\n"
            f"📊 លុយពីPOS (USD): ${result['pos_usd']:.2f}\n"
            f"➖ លើសឬបាត់លុយ (USD): ${result['diff_usd']:.4f}\n"
        )

        if safe_note:
            msg += f"\n📝 ចំណាំ:\n{safe_note}"

        ok = send_telegram_message(msg)
        if not ok:
            flash("Saved ✅ but Telegram failed. Check Render logs for exact Telegram error.")

    flash(f"Saved report #{report_id} ✅")
    return redirect(url_for("reports"))


@app.get("/reports")
def reports():
    conn = db()
    rows = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return render_template("reports.html", rows=rows)


@app.get("/reports/<int:report_id>")
def report_detail(report_id: int):
    conn = db()
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    denoms = conn.execute(
        "SELECT * FROM report_denoms WHERE report_id=? ORDER BY currency, denom",
        (report_id,)
    ).fetchall()
    conn.close()

    if not rep:
        return "Not found", 404

    return render_template("report_detail.html", rep=rep, denoms=denoms)


@app.get("/export.csv")
def export_csv():
    conn = db()
    rows = conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
    conn.close()

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow([
        "id", "created_at", "staff", "shift", "exchange_rate",
        "visa_usd", "aba_khr", "ac_usd", "ac_khr", "pos_usd",
        "expense_usd", "expense_khr",
        "cash_usd", "cash_khr", "total_khr", "total_usd", "note"
    ])
    for r in rows:
        w.writerow([r[k] for k in [
            "id", "created_at", "staff", "shift", "exchange_rate",
            "visa_usd", "aba_khr", "ac_usd", "ac_khr", "pos_usd",
            "expense_usd", "expense_khr",
            "cash_usd", "cash_khr", "total_khr", "total_usd", "note"
        ]])

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="system_details_reports.csv", mimetype="text/csv")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
