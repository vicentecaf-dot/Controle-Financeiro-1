from flask import Flask, request, redirect, url_for, render_template, flash
import sqlite3
from datetime import date, datetime, timedelta
import os

# Importação CSV Itaú cartão
import pandas as pd
import io
import unicodedata
import re

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
            cur.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (c,))

    conn.commit()
    conn.close()


def parse_date_any(x) -> date:
    if isinstance(x, (datetime, date)):
        return x if isinstance(x, date) else x.date()
    s = str(x).strip()
    if "/" in s:
        return datetime.strptime(s, "%d/%m/%Y").date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # mantém dia até 28 para evitar erro em meses menores
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


def norm_col(s: str) -> str:
    s = str(s).strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = s.replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    return s


def parse_brl_to_cents(x) -> int:
    """
    Aceita: 17.385,28 | -100,00 | 9.99 | -2741.88 | "R$ -54,00"
    Retorna centavos com sinal.
    """
    if x is None:
        return 0
    s = str(x).strip()
    s = s.replace("R$", "").replace(" ", "")

    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")

    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0
    return int(round(float(s) * 100))


@app.route("/")
def home():
    return redirect(url_for("month_view", ym=month_key(date.today())))


@app.route("/month/<ym>")
def month_view(ym):
    conn = get_conn()
    cur = conn.cursor()

    txs = cur.execute("""
        SELECT * FROM transactions
        WHERE substr(competence_date,1,7)=?
        ORDER BY competence_date DESC, id DESC
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

    cats = cur.execute("SELECT name FROM categories ORDER BY name").fetchall()
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
    try:
        d = parse_date_any(request.form["competence_date"])
        amount_cents = int(round(abs(float(request.form["amount"].replace(",", "."))) * 100))
        if amount_cents <= 0:
            raise ValueError("Valor inválido.")

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
            amount_cents
        ))
        conn.commit()
        conn.close()
        flash("Lançamento salvo ✅", "ok")
        return redirect(url_for("month_view", ym=month_key(d)))
    except Exception as e:
        flash(f"Erro: {e}", "err")
        return redirect(request.referrer or url_for("home"))


@app.route("/card/<ym>")
def card(ym):
    start, end = statement_period(ym)

    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM transactions
        WHERE payment_method='Cartão'
        AND competence_date>=?
        AND competence_date<=?
        ORDER BY competence_date DESC, id DESC
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


@app.route("/import-card", methods=["POST"])
def import_card_csv():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Nenhum arquivo enviado.", "err")
        return redirect(request.referrer or url_for("home"))

    raw = file.read()

    # CSV Itaú: encoding latin1 e separador pode ser ; ou ,
    try:
        df = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="latin1")
    except Exception:
        df = pd.read_csv(io.BytesIO(raw), sep=";", encoding="latin1")

    df.columns = [norm_col(c) for c in df.columns]

    # esperado: data | lancamento | valor
    if "data" not in df.columns or "lancamento" not in df.columns or "valor" not in df.columns:
        flash(f"CSV não reconhecido. Colunas encontradas: {list(df.columns)}", "err")
        return redirect(request.referrer or url_for("home"))

    conn = get_conn()
    cur = conn.cursor()

    imported, skipped = 0, 0
    for _, r in df.iterrows():
        desc = str(r.get("lancamento", "")).strip()
        if not desc:
            skipped += 1
            continue

        try:
            d = parse_date_any(r.get("data"))
        except Exception:
            skipped += 1
            continue

        cents = parse_brl_to_cents(r.get("valor"))
        if cents == 0:
            skipped += 1
            continue

        # Compra normalmente positiva => Despesa. Negativa => Receita (estorno/pagamento)
        ttype = "Receita" if cents < 0 else "Despesa"

        cur.execute("""
            INSERT INTO transactions
            (competence_date,type,category,description,payment_method,amount_cents)
            VALUES (?,?,?,?,?,?)
        """, (
            d.isoformat(),
            ttype,
            "Outros",
            desc,
            "Cartão",
            abs(cents)
        ))
        imported += 1

    conn.commit()
    conn.close()

    flash(f"Importação cartão concluída ✅ ({imported} itens, {skipped} ignorados)", "ok")
    return redirect(url_for("month_view", ym=month_key(date.today())))


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
