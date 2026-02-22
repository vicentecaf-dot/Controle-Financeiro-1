from flask import Flask, request, redirect, url_for, render_template, flash
import sqlite3
from datetime import date, datetime, timedelta
import os

APP_NAME = "Orçamento (Competência)"
DB_FILE = "finance.db"

CARD_DUE_DAY = 17
CARD_CLOSING_DAY = 10

DEFAULT_CATEGORIES = [
    "Moradia", "Alimentação", "Transporte", "Saúde",
    "Educação", "Lazer", "Assinaturas", "Impostos",
    "Compras", "Outros"
]

PAYMENT_METHODS = ["Cartão", "PIX", "Débito", "Boleto", "Dinheiro", "Transferência"]
TYPES = ["Despesa", "Receita"]

app = Flask(__name__)
app.secret_key = "mude-para-algo-seguro"


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        competence_date TEXT NOT NULL,
        type TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        amount_cents INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        name TEXT PRIMARY KEY
    )
    """)

    cur.execute("SELECT COUNT(*) as c FROM categories")
    if cur.fetchone()["c"] == 0:
        for c in DEFAULT_CATEGORIES:
            cur.execute("INSERT INTO categories(name) VALUES (?)", (c,))

    conn.commit()
    conn.close()


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, 28))


def statement_period(ym):
    y = int(ym[:4])
    m = int(ym[5:])
    closing = date(y, m, CARD_CLOSING_DAY)
    prev = add_months(closing, -1)
    start = prev + timedelta(days=1)
    return start, closing


def brl(cents):
    return f"R$ {cents/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.route("/")
def home():
    return redirect(url_for("month_view", ym=month_key(date.today())))
import pandas as pd

@app.route("/import", methods=["POST"])
def import_excel():
    file = request.files["file"]

    if not file:
        return "Nenhum arquivo enviado"

    df = pd.read_excel(file)

    conn = get_conn()
    cur = conn.cursor()

    # Espera colunas com estes nomes:
    # Data | Tipo | Categoria | Descrição | Método | Valor

    for _, row in df.iterrows():
        data = pd.to_datetime(row["Data"]).date()
        tipo = row["Tipo"]
        categoria = row["Categoria"]
        descricao = row["Descrição"]
        metodo = row["Método"]
        valor = float(row["Valor"])

        cur.execute("""
            INSERT INTO transactions
            (competence_date,type,category,description,payment_method,amount_cents)
            VALUES (?,?,?,?,?,?)
        """, (
            data.isoformat(),
            tipo,
            categoria,
            descricao,
            metodo,
            int(valor * 100)
        ))

    conn.commit()
    conn.close()

    return redirect(url_for("month_view", ym=month_key(date.today())))

@app.route("/month/<ym>")
def month_view(ym):
    conn = get_conn()
    cur = conn.cursor()

    txs = cur.execute("""
        SELECT * FROM transactions
        WHERE substr(competence_date,1,7)=?
        ORDER BY competence_date DESC
    """, (ym,)).fetchall()

    income = cur.execute("""
        SELECT COALESCE(SUM(amount_cents),0) v
        FROM transactions
        WHERE substr(competence_date,1,7)=? AND type='Receita'
    """, (ym,)).fetchone()["v"]

    expense = cur.execute("""
        SELECT COALESCE(SUM(amount_cents),0) v
        FROM transactions
        WHERE substr(competence_date,1,7)=? AND type='Despesa'
    """, (ym,)).fetchone()["v"]

    cats = cur.execute("SELECT name FROM categories").fetchall()
    conn.close()

    return render_template(
        "month.html",
        app_name=APP_NAME,
        ym=ym,
        txs=txs,
        income=income,
        expense=expense,
        balance=income-expense,
        categories=[c["name"] for c in cats],
        brl=brl,
        payment_methods=PAYMENT_METHODS,
        types=TYPES
    )


@app.route("/add", methods=["POST"])
def add_tx():
    d = parse_date(request.form["competence_date"])
    amount = int(float(request.form["amount"]) * 100)

    conn = get_conn()
    conn.execute("""
        INSERT INTO transactions
        (competence_date,type,category,description,payment_method,amount_cents)
        VALUES (?,?,?,?,?,?)
    """, (
        d.isoformat(),
        request.form["type"],
        request.form["category"],
        request.form["description"],
        request.form["payment_method"],
        amount
    ))
    conn.commit()
    conn.close()

    return redirect(url_for("month_view", ym=month_key(d)))


@app.route("/card/<ym>")
def card(ym):
    start, end = statement_period(ym)

    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM transactions
        WHERE payment_method='Cartão'
        AND competence_date>=?
        AND competence_date<=?
    """, (start.isoformat(), end.isoformat())).fetchall()
    total = sum(r["amount_cents"] for r in rows)
    conn.close()

    return render_template(
        "card.html",
        app_name=APP_NAME,
        ym=ym,
        rows=rows,
        total=total,
        start=start,
        end=end,
        brl=brl
    )


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
