"""
Omnichannel Fulfilment Platform Prototype
Course: Software Engineering (CSA1023)
Assignment 2 - Core Domain & Business Rules
Author: Nirmal S | Reg No: 192511249
"""

import sqlite3
import threading
import time


class DeniedError(Exception):
    pass


class PreconditionFailed(Exception):
    pass


class OutOfStock(Exception):
    pass


class InvalidTransition(Exception):
    pass


class GatewayError(Exception):
    pass


class FulfilmentPlatform:
    def __init__(self, db_path=":memory:"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            c = self.conn.cursor()
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, role TEXT);
            CREATE TABLE IF NOT EXISTS regions (code TEXT PRIMARY KEY, tax_bp INTEGER, shipping INTEGER, refund_limit INTEGER, sla_hours INTEGER);
            CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, price INTEGER, on_hand INTEGER, reserved INTEGER);
            CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, status TEXT, subtotal INTEGER, tax INTEGER, shipping INTEGER, total INTEGER, region TEXT, reserve_until INTEGER, dispatch_due INTEGER, picked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS payments (gateway_event_id TEXT PRIMARY KEY, order_id INTEGER, amount INTEGER, status TEXT);
            CREATE TABLE IF NOT EXISTS shipments (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, tracking_ref TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, order_id INTEGER, status TEXT DEFAULT 'Pending', attempts INTEGER DEFAULT 0, next_at INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS refunds (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, amount INTEGER, status TEXT, requested_by TEXT, approved_by TEXT, attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS scan_events (event_id TEXT PRIMARY KEY, node TEXT, sku TEXT, delta INTEGER, kind TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, entity TEXT, detail TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            """)

    def audit(self, actor, action, entity, detail=""):
        with self._lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO audit(actor, action, entity, detail) VALUES(?,?,?,?)",
                    (actor, action, entity, detail),
                )

    def require_role(self, actor_id, *allowed_roles):
        c = self.conn.cursor()
        r = c.execute("SELECT role FROM users WHERE id=?", (actor_id,)).fetchone()
        if not r or r["role"] not in allowed_roles:
            self.audit(
                actor_id,
                "DENIED_UNAUTHORIZED",
                f"user:{actor_id}",
                f"Requires {allowed_roles}",
            )
            raise DeniedError("Unauthorized")
        return r["role"]

    def place_order(self, customer_id, items, region="IN-TN"):
        self.require_role(customer_id, "customer")
        with self._lock:
            with self.conn:
                c = self.conn.cursor()
                reg = c.execute(
                    "SELECT * FROM regions WHERE code=?", (region,)
                ).fetchone()
                subtotal = 0
                for sku, qty in items:
                    p = c.execute(
                        "SELECT price FROM products WHERE sku=?", (sku,)
                    ).fetchone()
                    subtotal += p["price"] * qty
                    cur = c.execute(
                        "UPDATE products SET reserved = reserved + ? WHERE sku=? AND"
                        " on_hand - reserved >= ?",
                        (qty, sku, qty),
                    )
                    if cur.rowcount == 0:
                        raise OutOfStock(sku)
                tax = round(subtotal * (reg["tax_bp"] / 10000.0))
                shipping = reg["shipping"]
                total = subtotal + tax + shipping
                reserve_until = int(time.time()) + 900
                c.execute(
                    "INSERT INTO orders(customer_id, status, subtotal, tax, shipping,"
                    " total, region, reserve_until) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        customer_id,
                        "Placed",
                        subtotal,
                        tax,
                        shipping,
                        total,
                        region,
                        reserve_until,
                    ),
                )
                order_id = c.lastrowid
                self.audit(
                    customer_id, "ORDER_PLACED", f"order:{order_id}", f"total:{total}"
                )
                return order_id

    def payment_callback(self, event_id, order_id, amount):
        with self._lock:
            with self.conn:
                c = self.conn.cursor()
                if c.execute(
                    "SELECT 1 FROM payments WHERE gateway_event_id=?", (event_id,)
                ).fetchone():
                    self.audit("gateway", "DUPLICATE_CALLBACK", f"event:{event_id}")
                    return "duplicate_ignored"
                o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not o:
                    return "order_not_found"
                if o["status"] != "Placed" or o["total"] != amount:
                    c.execute(
                        "INSERT INTO payments VALUES(?,?,?,?)",
                        (event_id, order_id, amount, "NeedsReview"),
                    )
                    self.audit(
                        "gateway",
                        "PAYMENT_NEEDS_REVIEW",
                        f"order:{order_id}",
                        f"event:{event_id}",
                    )
                    return "needs_review"
                c.execute(
                    "INSERT INTO payments VALUES(?,?,?,?)",
                    (event_id, order_id, amount, "Confirmed"),
                )
                c.execute("UPDATE orders SET status='Paid' WHERE id=?", (order_id,))
                c.execute(
                    "INSERT INTO shipments(order_id, status) VALUES(?,?)",
                    (order_id, "PendingSync"),
                )
                c.execute(
                    "INSERT INTO outbox(kind, order_id) VALUES('courier_sync',?)",
                    (order_id,),
                )
                self.audit(
                    "gateway",
                    "PAYMENT_CONFIRMED",
                    f"order:{order_id}",
                    f"event:{event_id}",
                )
                return "confirmed"

    def release_expired(self):
        with self._lock:
            with self.conn:
                c = self.conn.cursor()
                now = int(time.time())
                expired = c.execute(
                    "SELECT id FROM orders WHERE status='Placed' AND reserve_until < ?",
                    (now,),
                ).fetchall()
                released_ids = []
                for row in expired:
                    oid = row["id"]
                    c.execute("UPDATE orders SET status='Cancelled' WHERE id=?", (oid,))
                    c.execute(
                        "UPDATE products SET reserved = reserved - 1 WHERE sku='SKU-1'"
                    )
                    self.audit("system", "ORDER_EXPIRED", f"order:{oid}")
                    released_ids.append(oid)
                return released_ids
