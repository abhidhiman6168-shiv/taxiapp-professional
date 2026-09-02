import os
from flask import Flask, request, redirect, url_for, session, render_template, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import requests
from functools import wraps


# =========================
# OSRM ROUTING
# =========================

def get_osrm_distance(pickup_lat, pickup_lng, destination_lat, destination_lng):
    try:
        url = (
            "https://router.project-osrm.org/route/v1/driving/"
            f"{pickup_lng},{pickup_lat};"
            f"{destination_lng},{destination_lat}"
        )

        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=10
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if data.get("code") != "Ok":
            return None

        routes = data.get("routes", [])

        if not routes:
            return None

        distance_km = routes[0]["distance"] / 1000

        return round(distance_km, 2)

    except Exception as e:
        print("OSRM Error:", e)
        return None


app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "private_uploads/kyc"
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max upload

app.secret_key = "82afa7e46f1d84448155b4606b1bf215c29cb3653ac30bcc6102b827f670de4e"

DATABASE = "taxi.db"

# =========================
# DATABASE
# =========================

def get_db():
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def create_notification(db, user_id, message, booking_id=None):
    db.execute(
        """
        INSERT INTO notifications
        (user_id, booking_id, message)
        VALUES (?, ?, ?)
        """,
        (user_id, booking_id, message)
    )


def get_fare_settings():
    db = get_db()

    rate_row = db.execute(
        "SELECT value FROM settings WHERE key = 'rate_per_km'"
    ).fetchone()

    minimum_row = db.execute(
        "SELECT value FROM settings WHERE key = 'minimum_fare'"
    ).fetchone()

    db.close()

    rate = float(rate_row["value"]) if rate_row else 15
    minimum_fare = float(minimum_row["value"]) if minimum_row else 0

    return rate, minimum_fare


def init_db():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mobile TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL,
            driver_id INTEGER,
            pickup TEXT NOT NULL,
            destination TEXT NOT NULL,
            distance REAL NOT NULL,
            fare REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'WAITING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT UNIQUE NOT NULL,
            driver_id INTEGER NOT NULL,
            rating INTEGER NOT NULL,
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT UNIQUE NOT NULL,
            fare REAL NOT NULL,
            commission REAL NOT NULL,
            driver_earning REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('commission_percent', '10')
    """)

    # Create notifications table
    db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            booking_id TEXT,
            message TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Auto-migrate users table for existing databases
    user_columns = {
        "vehicle": "TEXT DEFAULT ''",
        "blocked": "INTEGER DEFAULT 0",
        "approval": "TEXT DEFAULT 'approved'",
        "driving_license": "TEXT",
        "vehicle_rc": "TEXT",
        "id_proof": "TEXT",
        "driver_photo": "TEXT",
        "kyc_status": "TEXT DEFAULT 'pending'",
        "driving_license_file": "TEXT",
        "vehicle_rc_file": "TEXT",
        "id_proof_file": "TEXT",
        "latitude": "REAL",
        "longitude": "REAL",
        "location_updated_at": "TEXT"
    }

    existing_columns = {
        row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }

    for column, definition in user_columns.items():
        if column not in existing_columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

    db.commit()
    db.close()


# =========================
# LOGIN REQUIRED
# =========================

def login_required(role=None):

    def decorator(function):

        @wraps(function)
        def wrapper(*args, **kwargs):

            if "user_id" not in session:
                return redirect(url_for("login"))

            if role and session.get("role") != role:
                return "Access denied", 403

            return function(*args, **kwargs)

        return wrapper

    return decorator


# =========================
# HOME
# =========================

@app.route("/")
def home():

    if "user_id" in session:

        if session["role"] == "customer":
            return redirect(url_for("customer_dashboard"))

        if session["role"] == "driver":
            return redirect(url_for("driver_dashboard"))

        if session["role"] == "admin":
            return redirect(url_for("admin_dashboard"))

    return """
    <h1>🚕 TaxiApp</h1>
    <p>Professional Taxi Booking System</p>

    <a href="/login">Login</a>
    <br><br>
    <a href="/register">Register</a>
    """


# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"].strip()
        mobile = request.form["mobile"].strip()
        password = generate_password_hash(request.form["password"])
        role = request.form["role"]
        vehicle = request.form.get("vehicle", "").strip()

        if role not in ["customer", "driver"]:
            return "Invalid role"

        if not name or not mobile or not password:
            return "All fields are required"

        db = get_db()

        try:

            db.execute(
                """
                INSERT INTO users
                (name, mobile, password, role, vehicle, approval)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    mobile,
                    password,
                    role,
                    vehicle,
                    "pending" if role == "driver" else "approved"
                )
            )

            db.commit()

        except sqlite3.IntegrityError:

            db.close()

            return """
            <h3>❌ Mobile number already registered.</h3>
            <a href="/register">Try Again</a>
            """

        db.close()

        return redirect(url_for("login"))

    return render_template("register.html")


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        mobile = request.form["mobile"].strip()
        password = request.form["password"]

        db = get_db()

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE mobile = ?
            """,
            (mobile,)
        ).fetchone()

        if not user:
            db.close()

            return """
            <h3>❌ Wrong mobile number or password.</h3>
            <a href="/login">Try Again</a>
            """

        password_ok = False

        try:
            password_ok = check_password_hash(user["password"], password)
        except (ValueError, TypeError):
            password_ok = False

        if not password_ok and user["password"] == password:
            password_ok = True
            new_hash = generate_password_hash(password)
            db.execute(
                "UPDATE users SET password = ? WHERE id = ?",
                (new_hash, user["id"])
            )
            db.commit()

        if not password_ok:
            db.close()
            return """
            <h3>❌ Wrong mobile number or password.</h3>
            <a href="/login">Try Again</a>
            """

        if user["blocked"]:
            db.close()
            return """
            <h3>🚫 Your account is blocked.</h3>
            <p>Please contact Admin.</p>
            <a href="/login">Back to Login</a>
            """

        db.close()

        session["user_id"] = user["id"]
        session["name"] = user["name"]
        session["role"] = user["role"]

        if user["role"] == "customer":
            return redirect(url_for("customer_dashboard"))

        if user["role"] == "driver":
            return redirect(url_for("driver_dashboard"))

        if user["role"] == "admin":
            return redirect(url_for("admin_dashboard"))

    return render_template("login.html")


# =========================
# LOGOUT
# =========================

@app.route("/api/notifications")
@login_required()
def api_notifications():
    db = get_db()

    notifications = db.execute(
        """
        SELECT id, booking_id, message, is_read, created_at
        FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (session["user_id"],)
    ).fetchall()

    unread_count = db.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE user_id = ?
        AND is_read = 0
        """,
        (session["user_id"],)
    ).fetchone()[0]

    db.close()

    return jsonify({
        "unread_count": unread_count,
        "notifications": [dict(row) for row in notifications]
    })


@app.route("/notification/read/<int:notification_id>")
@login_required()
def mark_notification_read(notification_id):
    db = get_db()

    db.execute(
        """
        UPDATE notifications
        SET is_read = 1
        WHERE id = ?
        AND user_id = ?
        """,
        (notification_id, session["user_id"])
    )

    db.commit()
    db.close()

    if session.get("role") == "driver":
        return redirect(url_for("driver_dashboard"))

    return redirect(url_for("customer_dashboard"))


@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# =========================
# CUSTOMER DASHBOARD
# =========================

@app.route("/customer")
@login_required("customer")
def customer_dashboard():

    db = get_db()

    notifications = db.execute(
        """
        SELECT *
        FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (session["user_id"],)
    ).fetchall()

    bookings = db.execute(
        """
        SELECT bookings.*,
               users.name AS driver_name,
               users.mobile AS driver_mobile,
               users.latitude AS driver_latitude,
               users.longitude AS driver_longitude,
               users.location_updated_at AS driver_location_updated_at
        FROM bookings
        LEFT JOIN users
        ON bookings.driver_id = users.id
        WHERE bookings.customer_id = ?
        ORDER BY bookings.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    unread_count = db.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE user_id = ?
        AND is_read = 0
        """,
        (session["user_id"],)
    ).fetchone()[0]

    db.close()

    return render_template(
        "customer.html",
        bookings=bookings,
        notifications=notifications,
        unread_count=unread_count
    )


# =========================
# LOCATION GEOCODING
# =========================

def geocode_location(location):
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": location + ", India",
                "format": "json",
                "limit": 5,
                "countrycodes": "in"
            },
            headers={
                "User-Agent": "ProfessionalTaxiApp/1.0"
            },
            timeout=8
        )

        if response.status_code != 200:
            return None, None

        data = response.json()

        if not data:
            return None, None

        return float(data[0]["lat"]), float(data[0]["lon"])

    except Exception as e:
        print("Geocoding Error:", e)
        return None, None


# =========================
# BOOK TAXI
# =========================

@app.route("/book", methods=["GET", "POST"])
@login_required("customer")
def book_taxi():

    if request.method == "POST":

        pickup = request.form["pickup"].strip()
        destination = request.form["destination"].strip()
        payment_method = request.form.get("payment_method", "CASH").upper()

        if payment_method not in ["CASH", "ONLINE"]:
            return "Invalid payment method"

        # Customer GPS location
        try:
            pickup_latitude = float(request.form.get("pickup_latitude", ""))
            pickup_longitude = float(request.form.get("pickup_longitude", ""))
        except (ValueError, TypeError):
            pickup_latitude = None
            pickup_longitude = None

        if pickup_latitude is not None and not (-90 <= pickup_latitude <= 90):
            pickup_latitude = None

        if pickup_longitude is not None and not (-180 <= pickup_longitude <= 180):
            pickup_longitude = None

        # Destination GPS location
        try:
            destination_latitude = float(
                request.form.get("destination_latitude", "")
            )
            destination_longitude = float(
                request.form.get("destination_longitude", "")
            )
        except (ValueError, TypeError):
            destination_latitude = None
            destination_longitude = None

        if destination_latitude is not None and not (-90 <= destination_latitude <= 90):
            destination_latitude = None

        if destination_longitude is not None and not (-180 <= destination_longitude <= 180):
            destination_longitude = None

        # =========================
        # GEOCODING FALLBACK
        # =========================

        # Pickup GPS browser se nahi mila to text location se try karo
        if pickup_latitude is None or pickup_longitude is None:
            pickup_latitude, pickup_longitude = geocode_location(pickup)

        # Destination GPS browser se nahi mila to text location se try karo
        if destination_latitude is None or destination_longitude is None:
            destination_latitude, destination_longitude = geocode_location(destination)

        # =========================
        # OSRM ROAD DISTANCE
        # =========================

        print("BOOK DEBUG:", {"pickup": pickup, "destination": destination, "pickup_lat": pickup_latitude, "pickup_lng": pickup_longitude, "destination_lat": destination_latitude, "destination_lng": destination_longitude})
        distance = None

        if (
            pickup_latitude is not None
            and pickup_longitude is not None
            and destination_latitude is not None
            and destination_longitude is not None
        ):
            distance = get_osrm_distance(
                pickup_latitude,
                pickup_longitude,
                destination_latitude,
                destination_longitude
            )

        if distance is None:
            return "❌ Route distance calculate nahi ho paya. Pickup aur destination location check karein."

        if distance <= 0:
            return "Distance must be greater than 0"

        # =========================
        # FARE SETTINGS
        # =========================

        rate, minimum_fare = get_fare_settings()

        fare = distance * rate

        if fare < minimum_fare:
            fare = minimum_fare

        fare = round(fare, 2)

        db = get_db()

        cursor = db.execute(
            """
            INSERT INTO bookings
            (
                booking_id,
                customer_id,
                pickup,
                destination,
                distance,
                fare,
                status,
                payment_method,
                payment_status,
                pickup_latitude,
                pickup_longitude,
                destination_latitude,
                destination_longitude
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "TEMP",
                session["user_id"],
                pickup,
                destination,
                distance,
                fare,
                "WAITING",
                payment_method,
                "PENDING",
                pickup_latitude,
                pickup_longitude,
                destination_latitude,
                destination_longitude
            )
        )

        booking_number = cursor.lastrowid

        booking_id = "TAXI" + str(booking_number).zfill(5)

        db.execute(
            """
            UPDATE bookings
            SET booking_id = ?
            WHERE id = ?
            """,
            (booking_id, booking_number)
        )

        # =========================
        # NOTIFY AVAILABLE DRIVERS
        # =========================

        drivers = db.execute(
            """
            SELECT id
            FROM users
            WHERE role = 'driver'
            AND LOWER(approval) = 'approved'
            AND blocked = 0
            AND LOWER(kyc_status) = 'approved'
            """
        ).fetchall()

        for driver in drivers:
            create_notification(
                db,
                driver["id"],
                f"🚕 New booking available! Pickup: {pickup} → Destination: {destination} | Fare: ₹{fare} | Booking: {booking_id}",
                booking_id
            )

        db.commit()
        db.close()

        return redirect(url_for("customer_dashboard"))

    return render_template("book.html")


# =========================
# CANCEL BOOKING
# =========================

@app.route("/cancel/<booking_id>")
@login_required("customer")
def cancel_booking(booking_id):

    db = get_db()

    db.execute(
        """
        UPDATE bookings
        SET status = 'CANCELLED'
        WHERE booking_id = ?
        AND customer_id = ?
        AND status = 'WAITING'
        """,
        (booking_id, session["user_id"])
    )

    db.commit()
    db.close()

    return redirect(url_for("customer_dashboard"))


# =========================
# DRIVER DASHBOARD
# =========================

@app.route("/driver")
@login_required("driver")
def driver_dashboard():

    db = get_db()

    driver_status = db.execute(
        """
        SELECT approval, blocked, kyc_status
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (session["user_id"],)
    ).fetchone()

    available = db.execute(
        """
        SELECT bookings.*, users.name AS customer_name
        FROM bookings
        JOIN users
        ON bookings.customer_id = users.id
        WHERE bookings.status = 'WAITING'
        ORDER BY bookings.id DESC
        """
    ).fetchall()

    active = db.execute(
        """
        SELECT bookings.*, users.name AS customer_name
        FROM bookings
        JOIN users
        ON bookings.customer_id = users.id
        WHERE bookings.driver_id = ?
        AND bookings.status IN ('ACCEPTED', 'ON THE WAY')
        ORDER BY bookings.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    notifications = db.execute(
        """
        SELECT *
        FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (session["user_id"],)
    ).fetchall()

    unread_count = db.execute(
        """
        SELECT COUNT(*)
        FROM notifications
        WHERE user_id = ?
        AND is_read = 0
        """,
        (session["user_id"],)
    ).fetchone()[0]

    db.close()

    return render_template(
        "driver.html",
        available=available,
        active=active,
        driver_status=driver_status,
        notifications=notifications,
        unread_count=unread_count
    )


# =========================
# DRIVER PROFILE
# =========================

@app.route("/driver/profile")
@login_required("driver")
def driver_profile():

    db = get_db()

    driver = db.execute(
        """
        SELECT id, name, mobile, vehicle
        FROM users
        WHERE id = ? AND role = 'driver'
        """,
        (session["user_id"],)
    ).fetchone()

    stats = db.execute(
        """
        SELECT
            COUNT(*) AS completed_rides,
            COALESCE(SUM(fare), 0) AS total_fare
        FROM bookings
        WHERE driver_id = ?
        AND status = 'COMPLETED'
        """,
        (session["user_id"],)
    ).fetchone()

    rating_data = db.execute(
        """
        SELECT
            COALESCE(AVG(rating), 0) AS average_rating,
            COUNT(*) AS rating_count
        FROM ratings
        WHERE driver_id = ?
        """,
        (session["user_id"],)
    ).fetchone()

    reviews = db.execute(
        """
        SELECT rating, review, created_at
        FROM ratings
        WHERE driver_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    db.close()

    return render_template(
        "driver_profile.html",
        driver=driver,
        stats=stats,
        rating_data=rating_data,
        reviews=reviews
    )


# =========================
# ACCEPT RIDE
# =========================

@app.route("/accept/<booking_id>")
@login_required("driver")
def accept_booking(booking_id):

    db = get_db()

    driver = db.execute(
        """
        SELECT approval, blocked, kyc_status
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (session["user_id"],)
    ).fetchone()

    if not driver:
        db.close()
        return "Driver account not found", 404

    if driver["blocked"]:
        db.close()
        return """
        <h3>🚫 Your account is blocked.</h3>
        <p>You cannot accept rides.</p>
        <a href="/driver">Back to Driver Dashboard</a>
        """

    if driver["approval"] != "approved":
        db.close()
        return """
        <h3>🟡 Driver approval pending.</h3>
        <p>Admin approval is required before you can accept rides.</p>
        <a href="/driver">Back to Driver Dashboard</a>
        """

    if driver["kyc_status"] != "approved":
        db.close()
        return """
        <h3>🪪 KYC verification required.</h3>
        <p>Admin must verify your KYC before you can accept rides.</p>
        <a href="/driver/kyc">Complete KYC</a>
        """

    booking = db.execute(
        """
        SELECT customer_id
        FROM bookings
        WHERE booking_id = ?
        AND status = 'WAITING'
        """,
        (booking_id,)
    ).fetchone()

    if not booking:
        db.close()
        return "❌ Booking available nahi hai.", 404

    db.execute(
        """
        UPDATE bookings
        SET driver_id = ?,
            status = 'ACCEPTED'
        WHERE booking_id = ?
        AND status = 'WAITING'
        """,
        (session["user_id"], booking_id)
    )

    create_notification(
        db,
        booking["customer_id"],
        "🚕 Driver ne aapki booking accept kar li hai.",
        booking_id
    )

    db.commit()
    db.close()

    return redirect(url_for("driver_dashboard"))


# =========================
# ON THE WAY
# =========================

@app.route("/onway/<booking_id>")
@login_required("driver")
def on_the_way(booking_id):

    db = get_db()

    booking = db.execute(
        """
        SELECT customer_id
        FROM bookings
        WHERE booking_id = ?
        AND driver_id = ?
        AND status = 'ACCEPTED'
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking:
        db.close()
        return "❌ Booking available nahi hai.", 404

    db.execute(
        """
        UPDATE bookings
        SET status = 'ON THE WAY'
        WHERE booking_id = ?
        AND driver_id = ?
        AND status = 'ACCEPTED'
        """,
        (booking_id, session["user_id"])
    )

    create_notification(
        db,
        booking["customer_id"],
        "🚕 Driver aapki taraf aa raha hai. Ride ON THE WAY hai.",
        booking_id
    )

    db.commit()
    db.close()

    return redirect(url_for("driver_dashboard"))


# =========================
# COMPLETE RIDE
# =========================

@app.route("/complete/<booking_id>")
@login_required("driver")
def complete_ride(booking_id):

    db = get_db()

    booking = db.execute(
        """
        SELECT *
        FROM bookings
        WHERE booking_id = ?
        AND driver_id = ?
        AND status = 'ON THE WAY'
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking:

        db.close()

        return "Ride not found or not ready for completion"

    fare = booking["fare"]

    setting = db.execute(
        "SELECT value FROM app_settings WHERE key = 'commission_percent'"
    ).fetchone()

    commission_percent = float(setting["value"]) if setting else 10.0
    commission = fare * commission_percent / 100

    driver_earning = fare - commission

    db.execute(
        """
        UPDATE bookings
        SET status = 'COMPLETED'
        WHERE booking_id = ?
        """,
        (booking_id,)
    )

    create_notification(
        db,
        booking["customer_id"],
        "✅ Ride completed successfully. Thank you for using TaxiApp!",
        booking_id
    )

    db.execute(
        """
        INSERT INTO earnings
        (
            booking_id,
            fare,
            commission,
            driver_earning
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            booking_id,
            fare,
            commission,
            driver_earning
        )
    )

    db.commit()
    db.close()

    return redirect(url_for("driver_dashboard"))


# =========================
# CUSTOMER PAYMENT
# =========================

@app.route("/payment/<booking_id>", methods=["POST"])
@login_required("customer")
def make_payment(booking_id):

    db = get_db()

    booking = db.execute(
        """
        SELECT id, payment_method, payment_status, status
        FROM bookings
        WHERE booking_id = ?
        AND customer_id = ?
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking:
        db.close()
        return "Booking not found", 404

    if booking["status"] != "COMPLETED":
        db.close()
        return "Ride complete hone ke baad payment karein."

    if booking["payment_status"] == "PAID":
        db.close()
        return redirect(url_for("customer_dashboard"))

    db.execute(
        """
        UPDATE bookings
        SET payment_status = 'VERIFYING'
        WHERE booking_id = ?
        AND customer_id = ?
        """,
        (booking_id, session["user_id"])
    )

    db.commit()
    db.close()

    return redirect(url_for("customer_dashboard"))


# =========================
# ADMIN DASHBOARD
# =========================

# =========================
# RATE DRIVER
# =========================

@app.route("/rate/<booking_id>", methods=["GET", "POST"])
@login_required("customer")
def rate_driver(booking_id):

    db = get_db()

    booking = db.execute(
        """
        SELECT bookings.*, users.name AS driver_name
        FROM bookings
        LEFT JOIN users
        ON bookings.driver_id = users.id
        WHERE bookings.booking_id = ?
        AND bookings.customer_id = ?
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking:
        db.close()
        return "Booking not found"

    if booking["status"] != "COMPLETED":
        db.close()
        return "Sirf completed ride ko rate kar sakte ho."

    if booking["driver_id"] is None:
        db.close()
        return "Driver information nahi mili."

    existing = db.execute(
        """
        SELECT id
        FROM ratings
        WHERE booking_id = ?
        """,
        (booking_id,)
    ).fetchone()

    if existing:
        db.close()
        return "Ye ride already rated hai."

    if request.method == "POST":

        try:
            rating = int(request.form["rating"])
        except (ValueError, TypeError):
            db.close()
            return "Rating 1 se 5 ke beech honi chahiye."

        if rating < 1 or rating > 5:
            db.close()
            return "Rating 1 se 5 ke beech honi chahiye."

        review = request.form.get("review", "").strip()

        db.execute(
            """
            INSERT INTO ratings
            (
                booking_id,
                driver_id,
                rating,
                review
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                booking_id,
                booking["driver_id"],
                rating,
                review
            )
        )

        db.commit()
        db.close()

        return redirect(url_for("customer_dashboard"))

    db.close()

    return render_template(
        "rate.html",
        booking=booking
    )

@app.route("/admin")
@login_required("admin")
def admin_dashboard():

    db = get_db()

    total_bookings = db.execute(
        "SELECT COUNT(*) FROM bookings"
    ).fetchone()[0]

    completed = db.execute(
        """
        SELECT COUNT(*)
        FROM bookings
        WHERE status = 'COMPLETED'
        """
    ).fetchone()[0]

    total_fare = db.execute(
        "SELECT COALESCE(SUM(fare),0) FROM earnings"
    ).fetchone()[0]

    commission = db.execute(
        "SELECT COALESCE(SUM(commission),0) FROM earnings"
    ).fetchone()[0]

    drivers = db.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE role = 'driver'
        """
    ).fetchone()[0]

    customers = db.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE role = 'customer'
        """
    ).fetchone()[0]

    commission_row = db.execute(
        "SELECT value FROM app_settings WHERE key = 'commission_percent'"
    ).fetchone()

    commission_percent = float(
        commission_row["value"]
    ) if commission_row else 10.0

    driver_earnings = db.execute(
        """
        SELECT
            u.name AS driver_name,
            u.mobile AS driver_mobile,
            COALESCE(SUM(e.fare), 0) AS total_fare,
            COALESCE(SUM(e.commission), 0) AS commission,
            COALESCE(SUM(e.driver_earning), 0) AS driver_earning
        FROM users u
        LEFT JOIN bookings b
            ON u.id = b.driver_id
        LEFT JOIN earnings e
            ON b.booking_id = e.booking_id
        WHERE u.role = 'driver'
        GROUP BY u.id
        ORDER BY driver_earning DESC
    """
    ).fetchall()

    db.close()

    return render_template(
        "admin.html",
        total_bookings=total_bookings,
        completed=completed,
        total_fare=total_fare,
        commission=commission,
        commission_percent=commission_percent,
        drivers=drivers,
        customers=customers,
        driver_earnings=driver_earnings
    )


# =========================
# ADMIN FARE SETTINGS
# =========================

@app.route("/admin/fare-settings", methods=["GET", "POST"])
@login_required("admin")
def admin_fare_settings():

    db = get_db()

    if request.method == "POST":
        try:
            rate = float(request.form["rate_per_km"])
            minimum_fare = float(request.form["minimum_fare"])
        except (ValueError, TypeError):
            db.close()
            return "❌ Invalid fare value"

        if rate <= 0:
            db.close()
            return "❌ Rate per KM must be greater than 0"

        if minimum_fare < 0:
            db.close()
            return "❌ Minimum fare cannot be negative"

        db.execute(
            "UPDATE settings SET value = ? WHERE key = 'rate_per_km'",
            (str(rate),)
        )

        db.execute(
            "UPDATE settings SET value = ? WHERE key = 'minimum_fare'",
            (str(minimum_fare),)
        )

        db.commit()

    rate_row = db.execute(
        "SELECT value FROM settings WHERE key = 'rate_per_km'"
    ).fetchone()

    minimum_row = db.execute(
        "SELECT value FROM settings WHERE key = 'minimum_fare'"
    ).fetchone()

    commission_row = db.execute(
        "SELECT value FROM app_settings WHERE key = 'commission_percent'"
    ).fetchone()

    db.close()

    rate = float(rate_row["value"]) if rate_row else 15
    minimum_fare = float(minimum_row["value"]) if minimum_row else 0
    commission_percent = float(commission_row["value"]) if commission_row else 10

    return render_template(
        "fare_settings.html",
        rate=rate,
        minimum_fare=minimum_fare,
        commission_percent=commission_percent
    )


# =========================
# CREATE ADMIN
# =========================

def create_admin():

    db = get_db()

    admin = db.execute(
        """
        SELECT *
        FROM users
        WHERE role = 'admin'
        """
    ).fetchone()

    if not admin:

        db.execute(
            """
            INSERT INTO users
            (name, mobile, password, role)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Admin",
                "9999999999",
                generate_password_hash(os.environ.get("TAXI_ADMIN_PASSWORD", "")),
                "admin"
            )
        )

        db.commit()

    db.close()


# =========================
# ADMIN USERS
# =========================

@app.route("/admin/users")
@login_required("admin")
def admin_users():

    db = get_db()

    drivers = db.execute(
        """
        SELECT
            u.id,
            u.name,
            u.mobile,
            u.vehicle,
            u.blocked,
            u.approval,
            u.driving_license,
            u.vehicle_rc,
            u.id_proof,
            u.kyc_status,
            COALESCE(AVG(r.rating), 0) AS average_rating,
            COUNT(r.id) AS rating_count
        FROM users u
        LEFT JOIN ratings r ON u.id = r.driver_id
        WHERE u.role = 'driver'
        GROUP BY u.id
        ORDER BY u.id DESC
        """
    ).fetchall()

    customers = db.execute(
        """
        SELECT id, name, mobile, blocked, approval
        FROM users
        WHERE role = 'customer'
        ORDER BY id DESC
        """
    ).fetchall()

    db.close()

    return render_template(
        "admin_users.html",
        drivers=drivers,
        customers=customers
    )


# =========================
# ADMIN PAYMENT VERIFICATION
# =========================

@app.route("/admin/payments")
@login_required("admin")
def admin_payments():

    db = get_db()

    payments = db.execute(
        """
        SELECT
            b.booking_id,
            b.fare,
            b.payment_method,
            b.payment_status,
            b.paid_at,
            b.pickup,
            b.destination,
            c.name AS customer_name,
            c.mobile AS customer_mobile,
            d.name AS driver_name
        FROM bookings b
        LEFT JOIN users c ON b.customer_id = c.id
        LEFT JOIN users d ON b.driver_id = d.id
        WHERE b.payment_status = 'VERIFYING'
        ORDER BY b.id DESC
        """
    ).fetchall()

    db.close()

    return render_template(
        "admin_payments.html",
        payments=payments
    )


@app.route("/admin/payments/verify/<booking_id>")
@login_required("admin")
def verify_payment(booking_id):

    db = get_db()

    db.execute(
        """
        UPDATE bookings
        SET payment_status = 'PAID',
            paid_at = CURRENT_TIMESTAMP
        WHERE booking_id = ?
        AND payment_status = 'VERIFYING'
        """,
        (booking_id,)
    )

    db.commit()
    db.close()

    return redirect(url_for("admin_payments"))


# =========================
# ADMIN BLOCK / UNBLOCK USER
# =========================

@app.route("/admin/users/toggle/<int:user_id>")
@login_required("admin")
def toggle_user(user_id):

    db = get_db()

    user = db.execute(
        "SELECT id, role, blocked FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if not user:
        db.close()
        return "User not found"

    if user["role"] == "admin":
        db.close()
        return "Admin ko block nahi kar sakte."

    new_status = 0 if user["blocked"] else 1

    db.execute(
        "UPDATE users SET blocked = ? WHERE id = ?",
        (new_status, user_id)
    )

    db.commit()
    db.close()

    return redirect(url_for("admin_users"))


# =========================
# ADMIN DRIVER APPROVAL
# =========================

@app.route("/admin/users/approval/<int:user_id>/<action>")
@login_required("admin")
def driver_approval(user_id, action):

    if action not in ["approve", "reject"]:
        return "Invalid action", 400

    db = get_db()

    user = db.execute(
        "SELECT id, role FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if not user:
        db.close()
        return "User not found", 404

    if user["role"] != "driver":
        db.close()
        return "Sirf driver ka approval change kar sakte hain.", 400

    new_status = "approved" if action == "approve" else "rejected"

    db.execute(
        "UPDATE users SET approval = ? WHERE id = ?",
        (new_status, user_id)
    )

    db.commit()
    db.close()

    return redirect(url_for("admin_users"))


# =========================
# DRIVER KYC
# =========================

@app.route("/driver/kyc", methods=["GET", "POST"])
@login_required("driver")
def driver_kyc():

    db = get_db()

    if request.method == "POST":

        driving_license = request.form.get("driving_license", "").strip()
        vehicle_rc = request.form.get("vehicle_rc", "").strip()
        id_proof = request.form.get("id_proof", "").strip()

        if not driving_license or not vehicle_rc or not id_proof:
            db.close()
            return """
            <h3>❌ All KYC fields are required.</h3>
            <a href="/driver/kyc">Try Again</a>
            """

        upload_folder = app.config["UPLOAD_FOLDER"]

        files = {
            "driving_license_file": request.files.get("driving_license_file"),
            "vehicle_rc_file": request.files.get("vehicle_rc_file"),
            "id_proof_file": request.files.get("id_proof_file"),
            "driver_photo": request.files.get("driver_photo")
        }

        allowed_docs = {"jpg", "jpeg", "png", "pdf"}
        allowed_photo = {"jpg", "jpeg", "png"}

        for field, file in files.items():

            if not file or not file.filename:
                db.close()
                return f"""
                <h3>❌ {field} is required.</h3>
                <a href="/driver/kyc">Try Again</a>
                """

            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

            if field == "driver_photo":
                allowed = allowed_photo
            else:
                allowed = allowed_docs

            if ext not in allowed:
                db.close()
                return f"""
                <h3>❌ Invalid file type for {field}.</h3>
                <p>Allowed: {", ".join(sorted(allowed))}</p>
                <a href="/driver/kyc">Try Again</a>
                """

            # Verify actual file signature, not only the filename extension.
            file.stream.seek(0)
            header = file.stream.read(12)
            file.stream.seek(0)

            valid_signature = False

            if ext == "pdf":
                valid_signature = header.startswith(b"%PDF-")
            elif ext in {"jpg", "jpeg"}:
                valid_signature = header.startswith(b"\xff\xd8\xff")
            elif ext == "png":
                valid_signature = header.startswith(
                    b"\x89PNG\r\n\x1a\n"
                )

            if not valid_signature:
                db.close()
                return f"""
                <h3>❌ File content does not match its extension for {field}.</h3>
                <p>Please upload a valid {ext.upper()} file.</p>
                <a href="/driver/kyc">Try Again</a>
                """

        import uuid
        from werkzeug.utils import secure_filename

        user_id = session["user_id"]

        saved_files = {}

        for field, file in files.items():

            ext = file.filename.rsplit(".", 1)[-1].lower()
            filename = secure_filename(
                f"driver_{user_id}_{field}_{uuid.uuid4().hex[:8]}.{ext}"
            )

            path = os.path.join(upload_folder, filename)
            file.save(path)

            saved_files[field] = f"private_uploads/kyc/{filename}"

        db.execute(
            """
            UPDATE users
            SET driving_license = ?,
                vehicle_rc = ?,
                id_proof = ?,
                driving_license_file = ?,
                vehicle_rc_file = ?,
                id_proof_file = ?,
                driver_photo = ?,
                kyc_status = 'pending'
            WHERE id = ?
            AND role = 'driver'
            """,
            (
                driving_license,
                vehicle_rc,
                id_proof,
                saved_files["driving_license_file"],
                saved_files["vehicle_rc_file"],
                saved_files["id_proof_file"],
                saved_files["driver_photo"],
                user_id
            )
        )

        db.commit()
        db.close()

        return redirect(url_for("driver_profile"))

    driver = db.execute(
        """
        SELECT driving_license,
               vehicle_rc,
               id_proof,
               driving_license_file,
               vehicle_rc_file,
               id_proof_file,
               driver_photo,
               kyc_status
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (session["user_id"],)
    ).fetchone()

    db.close()

    return render_template(
        "driver_kyc.html",
        driver=driver
    )


@app.route("/admin/kyc-file/<int:user_id>/<file_type>")
@login_required("admin")
def admin_kyc_file(user_id, file_type):

    allowed = {
        "license": "driving_license_file",
        "rc": "vehicle_rc_file",
        "id": "id_proof_file",
        "photo": "driver_photo"
    }

    if file_type not in allowed:
        return "Invalid document type", 400

    db = get_db()

    row = db.execute(
        f"""
        SELECT {allowed[file_type]}
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (user_id,)
    ).fetchone()

    db.close()

    if not row or not row[0]:
        return "Document not found", 404

    relative_path = row[0]

    filename = Path(relative_path).name
    file_path = Path(app.config["UPLOAD_FOLDER"]) / filename

    if not file_path.exists():
        return "Uploaded file not found", 404

    from flask import send_from_directory

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        filename
    )


# =========================
# ADMIN KYC VERIFICATION
# =========================

@app.route("/admin/users/kyc/<int:user_id>/<action>")
@login_required("admin")
def kyc_approval(user_id, action):

    if action not in ["approve", "reject"]:
        return "Invalid action", 400

    db = get_db()

    user = db.execute(
        """
        SELECT id, role, driving_license, vehicle_rc, id_proof
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    if not user:
        db.close()
        return "User not found", 404

    if user["role"] != "driver":
        db.close()
        return "Sirf driver ka KYC verify kar sakte hain.", 400

    if action == "approve":
        status = "approved"
    else:
        status = "rejected"

    db.execute(
        """
        UPDATE users
        SET kyc_status = ?
        WHERE id = ?
        AND role = 'driver'
        """,
        (status, user_id)
    )

    db.commit()
    db.close()

    return redirect(url_for("admin_users"))


# =========================
# DRIVER LIVE LOCATION
# =========================

@app.route("/driver/location", methods=["POST"])
@login_required("driver")
def update_driver_location():

    data = request.get_json(silent=True) or {}

    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
        return {"success": False, "message": "Location missing"}, 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return {"success": False, "message": "Invalid location"}, 400

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {"success": False, "message": "Invalid coordinates"}, 400

    db = get_db()

    db.execute(
        """
        UPDATE users
        SET latitude = ?,
            longitude = ?,
            location_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        AND role = 'driver'
        """,
        (latitude, longitude, session["user_id"])
    )

    db.commit()
    db.close()

    return {
        "success": True,
        "message": "Location updated"
    }


# =========================
# CUSTOMER DRIVER LOCATION
# =========================

@app.route("/driver/location/<int:booking_id>")
@login_required("customer")
def get_driver_location(booking_id):

    db = get_db()

    booking = db.execute(
        """
        SELECT driver_id
        FROM bookings
        WHERE id = ?
        AND customer_id = ?
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking or not booking["driver_id"]:
        db.close()
        return {
            "success": False,
            "message": "Driver not assigned"
        }, 404

    driver = db.execute(
        """
        SELECT latitude, longitude, location_updated_at
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (booking["driver_id"],)
    ).fetchone()

    db.close()

    if not driver or driver["latitude"] is None or driver["longitude"] is None:
        return {
            "success": False,
            "message": "Driver location not available"
        }

    return {
        "success": True,
        "latitude": driver["latitude"],
        "longitude": driver["longitude"],
        "updated_at": driver["location_updated_at"]
    }


# =========================
# DRIVER DISTANCE + ETA
# =========================

@app.route("/driver/eta/<int:booking_id>")
@login_required("customer")
def driver_eta(booking_id):
    db = get_db()

    booking = db.execute(
        """
        SELECT pickup_latitude, pickup_longitude, driver_id
        FROM bookings
        WHERE id = ?
        AND customer_id = ?
        """,
        (booking_id, session["user_id"])
    ).fetchone()

    if not booking or not booking["driver_id"]:
        db.close()
        return {
            "success": False,
            "message": "Driver not assigned"
        }, 404

    if booking["pickup_latitude"] is None or booking["pickup_longitude"] is None:
        db.close()
        return {
            "success": False,
            "message": "Pickup location unavailable"
        }

    driver = db.execute(
        """
        SELECT latitude, longitude
        FROM users
        WHERE id = ?
        AND role = 'driver'
        """,
        (booking["driver_id"],)
    ).fetchone()

    db.close()

    if not driver or driver["latitude"] is None or driver["longitude"] is None:
        return {
            "success": False,
            "message": "Driver location not available"
        }

    try:
        driver_lat = float(driver["latitude"])
        driver_lng = float(driver["longitude"])
        pickup_lat = float(booking["pickup_latitude"])
        pickup_lng = float(booking["pickup_longitude"])

        url = (
            "https://router.project-osrm.org/route/v1/driving/"
            f"{driver_lng},{driver_lat};"
            f"{pickup_lng},{pickup_lat}"
        )

        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=10
        )

        if response.status_code != 200:
            return {
                "success": False,
                "message": "Routing service unavailable"
            }

        data = response.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return {
                "success": False,
                "message": "Route not available"
            }

        route = data["routes"][0]

        distance_km = route["distance"] / 1000
        duration_minutes = route["duration"] / 60

        return {
            "success": True,
            "distance_km": round(distance_km, 1),
            "eta_minutes": max(1, round(duration_minutes)),
            "pickup_latitude": pickup_lat,
            "pickup_longitude": pickup_lng,
            "route": route.get("geometry"),
        }

    except Exception as e:
        print("ETA ERROR:", e)
        return {
            "success": False,
            "message": "Unable to calculate ETA"
        }


# =========================
# INITIALIZE DATABASE FOR ALL SERVERS
# =========================
init_db()
create_admin()

# =========================
# START APP
# =========================

if __name__ == "__main__":

    init_db()
    create_admin()

    print("")
    print("🚕 PROFESSIONAL TAXI APP")
    print("----------------------------")
    print("🌐 http://127.0.0.1:5000")
    print("👑 Admin Mobile: 9999999999")
    print("🔑 Admin Password: [hidden]")
    print("----------------------------")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
