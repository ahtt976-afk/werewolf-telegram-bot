import os
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "saas_store_manager_secret_key_2026")

SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "admin2026")
IRAQ_TZ = timezone(timedelta(hours=3))


def iraq_now():
    return datetime.now(IRAQ_TZ)


DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_POSTGRES = bool(DATABASE_URL)
DB_NAME = "saas_stores.db"

if IS_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


class RowObj:
    """غلاف خفيف يسمح بالوصول للحقول بالنقطة أو الأقواس دون تعارض مع o.items في Jinja2."""
    def __init__(self, data):
        self._data = dict(data)

    def __getattr__(self, key):
        if key in self._data:
            return self._data[key]
        raise AttributeError(key)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)


class Database:
    def __init__(self):
        if IS_POSTGRES:
            self.conn = psycopg2.connect(DATABASE_URL)
            self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
        else:
            self.conn = sqlite3.connect(DB_NAME)
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()

    def execute(self, query, params=()):
        if IS_POSTGRES:
            query = query.replace('?', '%s')
        self.cursor.execute(query, params)
        return self.cursor

    def fetchone(self, query, params=()):
        return self.execute(query, params).fetchone()

    def fetchall(self, query, params=()):
        return self.execute(query, params).fetchall()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.cursor.close()
        self.conn.close()


def get_db():
    return Database()


def init_db():
    db = get_db()
    pk = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

    db.execute(f'''
        CREATE TABLE IF NOT EXISTS users (
            id {pk},
            store_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            subscription_end TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    db.execute(f'''
        CREATE TABLE IF NOT EXISTS products (
            id {pk},
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            cost_price DOUBLE PRECISION NOT NULL,
            sell_price DOUBLE PRECISION NOT NULL,
            stock INTEGER NOT NULL
        )
    ''')

    db.execute(f'''
        CREATE TABLE IF NOT EXISTS orders (
            id {pk},
            user_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            delivery_fee DOUBLE PRECISION NOT NULL,
            total_price DOUBLE PRECISION NOT NULL,
            profit DOUBLE PRECISION NOT NULL,
            status TEXT DEFAULT 'قيد التجهيز',
            created_at TEXT NOT NULL
        )
    ''')

    db.execute(f'''
        CREATE TABLE IF NOT EXISTS order_items (
            id {pk},
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            cost_price DOUBLE PRECISION NOT NULL,
            sell_price DOUBLE PRECISION NOT NULL
        )
    ''')

    db.execute(f'''
        CREATE TABLE IF NOT EXISTS customers (
            id {pk},
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            city TEXT NOT NULL,
            address TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    db.commit()
    db.close()


init_db()


def store_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))

        db = get_db()
        user = db.fetchone('SELECT * FROM users WHERE id = ?', (session['user_id'],))
        db.close()

        if not user:
            session.clear()
            return redirect(url_for('login'))

        session['store_name'] = user['store_name']
        session['subscription_end'] = user['subscription_end']

        today = iraq_now().strftime("%Y-%m-%d")
        if user['subscription_end'] < today:
            return redirect(url_for('subscription_expired'))

        return f(*args, **kwargs)
    return decorated_function


@app.route('/')
def index():
    return render_template('landing.html', is_logged_in=('user_id' in session))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        store_name = request.form.get('store_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        hashed_pw = generate_password_hash(password)
        now = iraq_now()
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        sub_end = (now + timedelta(days=7)).strftime("%Y-%m-%d")

        db = get_db()
        try:
            db.execute('''
                INSERT INTO users (store_name, email, password, subscription_end, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (store_name, email, hashed_pw, sub_end, created_at))
            db.commit()
            db.close()
            flash("تم إنشاء حساب متجرك بنجاح! يمكنك تسجيل الدخول الآن.", "success")
            return redirect(url_for('login'))
        except INTEGRITY_ERRORS:
            db.rollback()
            db.close()
            flash("هذا البريد الإلكتروني مسجل مسبقاً!", "error")

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        db = get_db()
        user = db.fetchone('SELECT * FROM users WHERE email = ?', (email,))
        db.close()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['store_name'] = user['store_name']
            session['subscription_end'] = user['subscription_end']
            return redirect(url_for('dashboard'))
        else:
            flash("البريد الإلكتروني أو كلمة المرور غير صحيحة!", "error")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('store_name', None)
    session.pop('subscription_end', None)
    return redirect(url_for('index'))


@app.route('/subscription-expired')
def subscription_expired():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    user = db.fetchone('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    db.close()

    if not user:
        session.clear()
        return redirect(url_for('login'))

    today = iraq_now().strftime("%Y-%m-%d")
    if user['subscription_end'] >= today:
        return redirect(url_for('dashboard'))

    return render_template(
        'expired.html',
        store_name=user['store_name'],
        email=user['email'],
        sub_end=user['subscription_end']
    )


@app.route('/dashboard')
@store_required
def dashboard():
    uid = session['user_id']
    period = request.args.get('period', 'all')
    if period not in ('week', 'month', 'all'):
        period = 'all'

    now = iraq_now()
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    if period == 'week':
        date_sql = " AND created_at >= ?"
        date_sql_o = " AND o.created_at >= ?"
        date_params = (week_start,)
    elif period == 'month':
        date_sql = " AND created_at >= ?"
        date_sql_o = " AND o.created_at >= ?"
        date_params = (month_start,)
    else:
        date_sql = ""
        date_sql_o = ""
        date_params = ()

    db = get_db()

    realized_profit = db.fetchone(
        f"SELECT COALESCE(SUM(profit), 0) AS val FROM orders WHERE user_id = ? AND status = 'واصل'{date_sql}",
        (uid,) + date_params
    )['val']

    total_sales = db.fetchone(
        f"SELECT COALESCE(SUM(total_price), 0) AS val FROM orders WHERE user_id = ? AND status = 'واصل'{date_sql}",
        (uid,) + date_params
    )['val']

    expected_profit = db.fetchone(
        f"SELECT COALESCE(SUM(profit), 0) AS val FROM orders WHERE user_id = ? AND status IN ('قيد التجهيز', 'قيد التوصيل'){date_sql}",
        (uid,) + date_params
    )['val']

    weekly_profit = db.fetchone(
        "SELECT COALESCE(SUM(profit), 0) AS val FROM orders WHERE user_id = ? AND status = 'واصل' AND created_at >= ?",
        (uid, week_start)
    )['val']

    monthly_profit = db.fetchone(
        "SELECT COALESCE(SUM(profit), 0) AS val FROM orders WHERE user_id = ? AND status = 'واصل' AND created_at >= ?",
        (uid, month_start)
    )['val']

    total_orders = db.fetchone(
        f"SELECT COUNT(*) AS val FROM orders WHERE user_id = ?{date_sql}",
        (uid,) + date_params
    )['val']

    delivered_count = db.fetchone(
        f"SELECT COUNT(*) AS val FROM orders WHERE user_id = ? AND status = 'واصل'{date_sql}",
        (uid,) + date_params
    )['val']

    returned_count = db.fetchone(
        f"SELECT COUNT(*) AS val FROM orders WHERE user_id = ? AND status = 'راجع'{date_sql}",
        (uid,) + date_params
    )['val']

    active_count = db.fetchone(
        f"SELECT COUNT(*) AS val FROM orders WHERE user_id = ? AND status IN ('قيد التجهيز', 'قيد التوصيل'){date_sql}",
        (uid,) + date_params
    )['val']

    top_selling = db.fetchall(f'''
        SELECT oi.product_name,
               SUM(oi.quantity) AS total_qty,
               COUNT(DISTINCT oi.order_id) AS orders_count
        FROM order_items oi
        INNER JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = ? AND o.status = 'واصل'{date_sql_o}
        GROUP BY oi.product_name
        ORDER BY total_qty DESC
        LIMIT 5
    ''', (uid,) + date_params)

    top_returned = db.fetchall(f'''
        SELECT oi.product_name,
               SUM(oi.quantity) AS total_qty,
               COUNT(DISTINCT oi.order_id) AS orders_count
        FROM order_items oi
        INNER JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = ? AND o.status = 'راجع'{date_sql_o}
        GROUP BY oi.product_name
        ORDER BY total_qty DESC
        LIMIT 5
    ''', (uid,) + date_params)

    low_stock = db.fetchall(
        "SELECT * FROM products WHERE user_id = ? AND stock <= 3 ORDER BY stock ASC", (uid,)
    )

    raw_recent = db.fetchall('''
        SELECT orders.*, COALESCE(products.name, 'منتج محذوف') AS product_name
        FROM orders
        LEFT JOIN products ON orders.product_id = products.id
        WHERE orders.user_id = ?
        ORDER BY orders.id DESC
        LIMIT 5
    ''', (uid,))

    recent_orders = []
    for row in raw_recent:
        order_obj = RowObj(row)
        order_items = db.fetchall(
            'SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC',
            (order_obj['id'],)
        )
        if order_items:
            order_obj['items'] = [RowObj(it) for it in order_items]
            order_obj['product_name'] = ' + '.join(it['product_name'] for it in order_items)
        else:
            order_obj['items'] = []
        recent_orders.append(order_obj)

    db.close()

    return render_template(
        'dashboard.html',
        store_name=session['store_name'],
        sub_end=session['subscription_end'],
        period=period,
        realized_profit=realized_profit,
        expected_profit=expected_profit,
        weekly_profit=weekly_profit,
        monthly_profit=monthly_profit,
        total_sales=total_sales,
        total_orders=total_orders,
        delivered_count=delivered_count,
        returned_count=returned_count,
        active_count=active_count,
        top_selling=top_selling,
        top_returned=top_returned,
        low_stock=low_stock,
        recent_orders=recent_orders
    )


@app.route('/products')
@store_required
def products():
    db = get_db()
    user_products = db.fetchall(
        'SELECT * FROM products WHERE user_id = ? ORDER BY id DESC',
        (session['user_id'],)
    )
    db.close()

    return render_template(
        'products.html',
        products=user_products,
        store_name=session['store_name'],
        sub_end=session['subscription_end']
    )


@app.route('/products/add', methods=['POST'])
@store_required
def add_product():
    name = request.form.get('name', '').strip()
    cost_price = float(request.form.get('cost_price'))
    sell_price = float(request.form.get('sell_price'))
    stock = int(request.form.get('stock'))

    db = get_db()
    db.execute('''
        INSERT INTO products (user_id, name, cost_price, sell_price, stock)
        VALUES (?, ?, ?, ?, ?)
    ''', (session['user_id'], name, cost_price, sell_price, stock))
    db.commit()
    db.close()

    flash("تمت إضافة المنتج إلى المخزن بنجاح!", "success")
    return redirect(url_for('products'))


@app.route('/products/restock/<int:product_id>', methods=['POST'])
@store_required
def restock_product(product_id):
    try:
        add_qty = int(request.form.get('add_qty', 0))
    except ValueError:
        add_qty = 0

    if add_qty <= 0:
        flash("يرجى إدخال كمية صحيحة أكبر من صفر.", "error")
        return redirect(url_for('products'))

    uid = session['user_id']
    db = get_db()
    product = db.fetchone(
        'SELECT * FROM products WHERE id = ? AND user_id = ?',
        (product_id, uid)
    )
    if not product:
        db.close()
        flash("المنتج غير موجود في مخزنك!", "error")
        return redirect(url_for('products'))

    db.execute(
        'UPDATE products SET stock = stock + ? WHERE id = ? AND user_id = ?',
        (add_qty, product_id, uid)
    )
    db.commit()
    new_stock = product['stock'] + add_qty
    db.close()

    flash(f"تمت إضافة {add_qty} قطعة إلى ({product['name']}). الرصيد المتوفر الآن: {new_stock}.", "success")
    return redirect(url_for('products'))


@app.route('/products/delete/<int:product_id>', methods=['POST'])
@store_required
def delete_product(product_id):
    db = get_db()
    db.execute(
        'DELETE FROM products WHERE id = ? AND user_id = ?',
        (product_id, session['user_id'])
    )
    db.commit()
    db.close()

    flash("تم حذف المنتج من المخزن.", "success")
    return redirect(url_for('products'))


@app.route('/orders')
@store_required
def orders():
    uid = session['user_id']
    db = get_db()
    available_products = db.fetchall(
        'SELECT * FROM products WHERE user_id = ? AND stock > 0 ORDER BY name ASC',
        (uid,)
    )

    saved_customers = db.fetchall(
        'SELECT * FROM customers WHERE user_id = ? ORDER BY name ASC, id DESC',
        (uid,)
    )

    raw_orders = db.fetchall('''
        SELECT orders.*, COALESCE(products.name, 'منتج محذوف') AS product_name
        FROM orders
        LEFT JOIN products ON orders.product_id = products.id
        WHERE orders.user_id = ?
        ORDER BY orders.id DESC
    ''', (uid,))

    all_items = db.fetchall('''
        SELECT oi.*
        FROM order_items oi
        INNER JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = ?
        ORDER BY oi.id ASC
    ''', (uid,))

    items_by_order = {}
    for item in all_items:
        items_by_order.setdefault(item['order_id'], []).append(RowObj(item))

    user_orders = []
    for row in raw_orders:
        order_obj = RowObj(row)
        order_obj['items'] = items_by_order.get(order_obj['id'], [])
        user_orders.append(order_obj)

    db.close()

    return render_template(
        'orders.html',
        orders=user_orders,
        products=available_products,
        saved_customers=saved_customers,
        store_name=session['store_name'],
        sub_end=session['subscription_end']
    )


@app.route('/orders/add', methods=['POST'])
@store_required
def add_order():
    uid = session['user_id']
    product_ids_raw = request.form.getlist('product_id[]') or request.form.getlist('product_id')
    quantities_raw = request.form.getlist('quantity[]') or request.form.getlist('quantity')

    customer_name = request.form.get('customer_name', '').strip()
    phone = request.form.get('phone', '').strip()
    city = request.form.get('city', '').strip()
    address = request.form.get('address', '').strip()
    save_customer = request.form.get('save_customer') == '1'

    try:
        delivery_fee = float(request.form.get('delivery_fee') or 0)
    except ValueError:
        delivery_fee = 0.0

    if delivery_fee < 0:
        flash("أجور التوصيل غير صالحة!", "error")
        return redirect(url_for('orders'))

    if not product_ids_raw or len(product_ids_raw) != len(quantities_raw):
        flash("يرجى اختيار منتج واحد على الأقل مع كمية صحيحة!", "error")
        return redirect(url_for('orders'))

    # دمج الكميات إذا تكرر نفس المنتج في أكثر من صف داخل نفس الطلب
    merged_items = {}
    try:
        for pid_str, qty_str in zip(product_ids_raw, quantities_raw):
            pid = int(pid_str)
            qty = int(qty_str)
            if qty <= 0:
                flash("يجب أن تكون الكمية 1 على الأقل لكل منتج!", "error")
                return redirect(url_for('orders'))
            merged_items[pid] = merged_items.get(pid, 0) + qty
    except ValueError:
        flash("بيانات المنتجات أو الكميات غير صالحة!", "error")
        return redirect(url_for('orders'))

    db = get_db()
    resolved_items = []
    subtotal = 0.0
    total_profit = 0.0
    total_qty = 0

    for pid, qty in merged_items.items():
        product = db.fetchone(
            'SELECT * FROM products WHERE id = ? AND user_id = ?',
            (pid, uid)
        )
        if not product:
            db.close()
            flash("أحد المنتجات المختارة غير موجود في مخزنك!", "error")
            return redirect(url_for('orders'))

        if product['stock'] < qty:
            db.close()
            flash(
                f"الكمية غير متوفرة للمنتج ({product['name']})! المتبقي حالياً: {product['stock']} قطعة.",
                "error"
            )
            return redirect(url_for('orders'))

        subtotal += product['sell_price'] * qty
        total_profit += (product['sell_price'] - product['cost_price']) * qty
        total_qty += qty
        resolved_items.append({
            'product_id': product['id'],
            'product_name': product['name'],
            'quantity': qty,
            'cost_price': product['cost_price'],
            'sell_price': product['sell_price']
        })

    total_price = subtotal + delivery_fee
    first_product_id = resolved_items[0]['product_id']
    created_at = iraq_now().strftime("%Y-%m-%d %H:%M")

    if IS_POSTGRES:
        order_id = db.fetchone('''
            INSERT INTO orders (user_id, product_id, customer_name, phone, city, address, quantity, delivery_fee, total_price, profit, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'قيد التجهيز', ?)
            RETURNING id
        ''', (uid, first_product_id, customer_name, phone, city, address, total_qty, delivery_fee, total_price, total_profit, created_at))['id']
    else:
        cur = db.execute('''
            INSERT INTO orders (user_id, product_id, customer_name, phone, city, address, quantity, delivery_fee, total_price, profit, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'قيد التجهيز', ?)
        ''', (uid, first_product_id, customer_name, phone, city, address, total_qty, delivery_fee, total_price, total_profit, created_at))
        order_id = cur.lastrowid

    for item in resolved_items:
        db.execute('''
            INSERT INTO order_items (order_id, product_id, product_name, quantity, cost_price, sell_price)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (order_id, item['product_id'], item['product_name'], item['quantity'], item['cost_price'], item['sell_price']))

        db.execute(
            'UPDATE products SET stock = stock - ? WHERE id = ? AND user_id = ?',
            (item['quantity'], item['product_id'], uid)
        )

    if save_customer and customer_name and phone:
        existing_customer = db.fetchone(
            'SELECT id FROM customers WHERE user_id = ? AND phone = ?',
            (uid, phone)
        )
        if existing_customer:
            db.execute(
                'UPDATE customers SET name = ?, city = ?, address = ? WHERE id = ? AND user_id = ?',
                (customer_name, city, address, existing_customer['id'], uid)
            )
        else:
            db.execute('''
                INSERT INTO customers (user_id, name, phone, city, address, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (uid, customer_name, phone, city, address, created_at))

    db.commit()
    db.close()

    flash(f"تم تسجيل الطلب #{order_id} وخصم الكميات من المخزن بنجاح!", "success")
    return redirect(url_for('orders'))


@app.route('/customers/delete/<int:customer_id>', methods=['POST'])
@store_required
def delete_customer(customer_id):
    db = get_db()
    db.execute(
        'DELETE FROM customers WHERE id = ? AND user_id = ?',
        (customer_id, session['user_id'])
    )
    db.commit()
    db.close()
    flash("تم حذف الزبون من قائمة الزبائن الدائمين.", "success")
    return redirect(url_for('orders'))


@app.route('/orders/status/<int:order_id>', methods=['POST'])
@store_required
def update_order_status(order_id):
    new_status = request.form.get('status')
    valid_statuses = ['قيد التجهيز', 'قيد التوصيل', 'واصل', 'راجع']
    if new_status not in valid_statuses:
        return redirect(url_for('orders'))

    uid = session['user_id']
    db = get_db()
    order = db.fetchone(
        'SELECT * FROM orders WHERE id = ? AND user_id = ?',
        (order_id, uid)
    )

    if order and order['status'] != new_status:
        old_status = order['status']
        order_items = db.fetchall(
            'SELECT * FROM order_items WHERE order_id = ?',
            (order_id,)
        )

        # تجهيز قائمة المنتجات للتعديل على المخزن (يدعم الطلبات الجديدة والقديمة)
        stock_targets = []
        if order_items:
            for it in order_items:
                if it['product_id']:
                    stock_targets.append((it['product_id'], it['quantity']))
        elif order['product_id']:
            stock_targets.append((order['product_id'], order['quantity']))

        if old_status != 'راجع' and new_status == 'راجع':
            for pid, qty in stock_targets:
                db.execute(
                    'UPDATE products SET stock = stock + ? WHERE id = ? AND user_id = ?',
                    (qty, pid, uid)
                )
        elif old_status == 'راجع' and new_status != 'راجع':
            for pid, qty in stock_targets:
                db.execute(
                    'UPDATE products SET stock = CASE WHEN stock - ? < 0 THEN 0 ELSE stock - ? END WHERE id = ? AND user_id = ?',
                    (qty, qty, pid, uid)
                )

        db.execute(
            'UPDATE orders SET status = ? WHERE id = ? AND user_id = ?',
            (new_status, order_id, uid)
        )
        db.commit()
        flash(f"تم تغيير حالة الطلب #{order_id} إلى ({new_status}).", "success")

    db.close()
    return redirect(url_for('orders'))


@app.route('/orders/delete/<int:order_id>', methods=['POST'])
@store_required
def delete_order(order_id):
    uid = session['user_id']
    db = get_db()
    order = db.fetchone('SELECT id FROM orders WHERE id = ? AND user_id = ?', (order_id, uid))
    if order:
        db.execute('DELETE FROM order_items WHERE order_id = ?', (order_id,))
        db.execute('DELETE FROM orders WHERE id = ? AND user_id = ?', (order_id, uid))
        db.commit()
        flash("تم حذف الطلب نهائياً.", "success")
    db.close()
    return redirect(url_for('orders'))


@app.route('/orders/invoice/<int:order_id>')
@store_required
def order_invoice(order_id):
    db = get_db()
    order = db.fetchone('''
        SELECT orders.*, COALESCE(products.name, 'منتج محذوف') AS product_name
        FROM orders
        LEFT JOIN products ON orders.product_id = products.id
        WHERE orders.id = ? AND orders.user_id = ?
    ''', (order_id, session['user_id']))

    if not order:
        db.close()
        flash("الوصل غير موجود!", "error")
        return redirect(url_for('orders'))

    raw_items = db.fetchall(
        'SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC',
        (order_id,)
    )
    items = [RowObj(it) for it in raw_items]
    db.close()

    subtotal = order['total_price'] - order['delivery_fee']
    return render_template(
        'invoice.html',
        order=RowObj(order),
        items=items,
        subtotal=subtotal,
        store_name=session['store_name']
    )


@app.route('/settings', methods=['GET', 'POST'])
@store_required
def account_settings():
    uid = session['user_id']
    db = get_db()
    user = db.fetchone('SELECT * FROM users WHERE id = ?', (uid,))

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not check_password_hash(user['password'], current_password):
            db.close()
            flash("كلمة المرور الحالية غير صحيحة!", "error")
            return redirect(url_for('account_settings'))

        if len(new_password) < 6:
            db.close()
            flash("يجب أن تتكون كلمة المرور الجديدة من 6 أحرف أو أرقام على الأقل.", "error")
            return redirect(url_for('account_settings'))

        if new_password != confirm_password:
            db.close()
            flash("كلمة المرور الجديدة وتأكيدها غير متطابقين!", "error")
            return redirect(url_for('account_settings'))

        if current_password == new_password:
            db.close()
            flash("كلمة المرور الجديدة مطابقة للحالية، يرجى اختيار كلمة مختلفة.", "error")
            return redirect(url_for('account_settings'))

        hashed_pw = generate_password_hash(new_password)
        db.execute('UPDATE users SET password = ? WHERE id = ?', (hashed_pw, uid))
        db.commit()
        db.close()
        flash("تم تغيير كلمة المرور بنجاح!", "success")
        return redirect(url_for('account_settings'))

    db.close()
    return render_template(
        'settings.html',
        store_name=user['store_name'],
        email=user['email'],
        sub_end=user['subscription_end']
    )


@app.route('/superadmin/login', methods=['GET', 'POST'])
def superadmin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == SUPERADMIN_PASSWORD:
            session['is_superadmin'] = True
            return redirect(url_for('superadmin_dashboard'))
        else:
            flash("كلمة مرور الإدارة غير صحيحة!", "error")
    return render_template('superadmin_login.html')


@app.route('/superadmin')
def superadmin_dashboard():
    if not session.get('is_superadmin'):
        return redirect(url_for('superadmin_login'))

    today = iraq_now().strftime("%Y-%m-%d")
    db = get_db()
    stores = db.fetchall('''
        SELECT users.*,
               (SELECT COUNT(*) FROM orders WHERE orders.user_id = users.id) AS orders_count,
               (SELECT COUNT(*) FROM products WHERE products.user_id = users.id) AS products_count
        FROM users
        ORDER BY users.id DESC
    ''')
    total_platform_orders = db.fetchone('SELECT COUNT(*) AS val FROM orders')['val']
    db.close()

    active_stores = sum(1 for s in stores if s['subscription_end'] >= today)
    expired_stores = len(stores) - active_stores

    return render_template(
        'superadmin.html',
        stores=stores,
        today=today,
        active_stores=active_stores,
        expired_stores=expired_stores,
        total_platform_orders=total_platform_orders
    )


@app.route('/superadmin/extend/<int:user_id>', methods=['POST'])
def superadmin_extend(user_id):
    if not session.get('is_superadmin'):
        return redirect(url_for('superadmin_login'))

    try:
        days = int(request.form.get('days', 30))
        if days <= 0:
            days = 30
    except ValueError:
        days = 30

    db = get_db()
    user = db.fetchone('SELECT * FROM users WHERE id = ?', (user_id,))
    if user:
        now = iraq_now()
        today_str = now.strftime("%Y-%m-%d")
        if user['subscription_end'] >= today_str:
            base_date = datetime.strptime(user['subscription_end'], "%Y-%m-%d")
        else:
            base_date = now

        new_end = (base_date + timedelta(days=days)).strftime("%Y-%m-%d")
        db.execute('UPDATE users SET subscription_end = ? WHERE id = ?', (new_end, user_id))
        db.commit()
        label = "سنة كاملة (365 يوماً)" if days == 365 else f"{days} يوماً"
        flash(f"تم تمديد اشتراك ({user['store_name']}) لمدة {label} لغاية {new_end} بنجاح!", "success")
    db.close()
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/reset_password/<int:user_id>', methods=['POST'])
def superadmin_reset_password(user_id):
    if not session.get('is_superadmin'):
        return redirect(url_for('superadmin_login'))

    new_password = request.form.get('new_password', '').strip()
    if len(new_password) < 6:
        flash("يجب أن تتكون كلمة المرور الجديدة من 6 أحرف أو أرقام على الأقل.", "error")
        return redirect(url_for('superadmin_dashboard'))

    db = get_db()
    user = db.fetchone('SELECT * FROM users WHERE id = ?', (user_id,))
    if user:
        hashed_pw = generate_password_hash(new_password)
        db.execute('UPDATE users SET password = ? WHERE id = ?', (hashed_pw, user_id))
        db.commit()
        flash(f"تم تعيين كلمة مرور جديدة لمتجر ({user['store_name']}) بنجاح.", "success")
    db.close()
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/suspend/<int:user_id>', methods=['POST'])
def superadmin_suspend(user_id):
    if not session.get('is_superadmin'):
        return redirect(url_for('superadmin_login'))

    yesterday = (iraq_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    db = get_db()
    user = db.fetchone('SELECT * FROM users WHERE id = ?', (user_id,))
    if user:
        db.execute('UPDATE users SET subscription_end = ? WHERE id = ?', (yesterday, user_id))
        db.commit()
        flash(f"تم إيقاف وقفل حساب ({user['store_name']}) فوراً.", "success")
    db.close()
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/delete/<int:user_id>', methods=['POST'])
def superadmin_delete(user_id):
    if not session.get('is_superadmin'):
        return redirect(url_for('superadmin_login'))

    db = get_db()
    db.execute('DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE user_id = ?)', (user_id,))
    db.execute('DELETE FROM orders WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM products WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM customers WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    db.close()
    flash("تم حذف المتجر وكافة بياناته نهائياً.", "success")
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/logout')
def superadmin_logout():
    session.pop('is_superadmin', None)
    return redirect(url_for('superadmin_login'))


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=not IS_POSTGRES)
