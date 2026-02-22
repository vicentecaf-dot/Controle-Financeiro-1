from flask import Flask, request, redirect, url_for, render_template, flash
import sqlite3
from datetime import date, datetime, timedelta
import os
import csv
import unicodedata
import re

APP_NAME = "Orçamento (Competência)"
DB_FILE = "finance.db"

# Cartão (Itaú): fecha dia 10, vence dia 17
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


# -------------------------
# DB
# -------------------------
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
        competence_date TEXT NOT NULL,   -- YYYY-MM-DD
        type TEXT NOT NULL,             -- Despesa/Receita
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        amount_cents INTEGER NOT NULL    -- sempre positivo
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


# -------------------------
# Helpers
# -------------------------
def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # evita erro em meses curtos
    return date(y, m, min(d.day, 28))


def statement_period(ym: str):
    """Período de fatura do mês ym (mês do vencimento)"""
    y = int(ym[:4])
    m = int(ym[5:])
    closing = date(y, m, CARD_CLOSING_DAY)
    prev = add_months(closing, -1)
    start = prev + timedelta(days=1)
    return start, closing


def brl(cents: int) -> str:
    """Formata centavos (pode ser negativo) em BRL"""
    s = f"{cents/100:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def norm_col(s: str) -> str:
    """Normaliza nomes de colunas: remove acentos e padroniza"""
    s = str(s).strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = s.replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    return s


def parse_date_any(x) -> date:
    """Aceita dd/mm/yyyy ou yyyy-mm-dd"""
    s = str(x).strip()
    if "/" in s:
        return datetime.strptime(s, "%d/%m/%Y").date()
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_amount_input_to_cents(x: str) -> int:
    """Converte valor digitado no formulário (1.234,56 ou 1234.56) para centavos (positivo)"""
    s = str(x).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0
    return int(round(abs(float(s)) * 100))


def parse_brl_to_cents_signed(x) -> int:
    """
    Para CSV Itaú: aceita -100,00 / 17.385,28 / 9,99 / 9.99 etc.
    Retorna centavos COM SINAL.
    """
    if x is None:
        return 0
    s = str(x).strip().replace("R$", "").replace(" ", "")

    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")

    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0
    return int(round(float(s) * 100))


# -------------------------
# Routes
# -------------------------
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
        balance=income - expense,
        categories=[c["name"] for c in cats],
        brl=brl,
        payment_methods=PAYMENT_METHODS,
        types=TYPES
    )


@app.route("/add", methods=["POST"])
def add_tx():
    try:
        d = parse_date_any(request.form["competence_date"])
        ttype = request.form["type"]
        category = request.form["category"]
        description = request.form["description"].strip()
        method = request.form["payment_method"]
        amount_cents = parse_amount_input_to_cents(request.form["amount"])

        if amount_cents <= 0:
            raise ValueError("Valor inválido.")
        if ttype not in TYPES:
            raise ValueError("Tipo inválido.")
        if method not in PAYMENT_METHODS:
            raise ValueError("Método inválido.")
        if not description:
            raise ValueError("Descrição obrigatória.")

        conn = get_conn()
        conn.execute("""
            INSERT INTO transactions
            (competence_date,type,category,description,payment_method,amount_cents)
            VALUES (?,?,?,?,?,?)
        """, (d.isoformat(), ttype, category, description, method, amount_cents))
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

    total = sum(r["amount_cents"] for r in rows if r["type"] == "Despesa") - \
            sum(r["amount_cents"] for r in rows if r["type"] == "Receita")

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
    """
    Importa CSV do cartão Itaú com colunas tipo:
    data,lançamento,valor
    (pode ser ; ou , e encoding latin1)
    """
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Nenhum arquivo enviado.", "err")
        return redirect(request.referrer or url_for("home"))

    raw = file.read()
    text = raw.decode("latin1", errors="ignore")

    # Detecta delimitador (Itaú pode ser ; ou ,)
    sample = text[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","

    reader = csv.DictReader(text.splitlines(), delimiter=delim)

    if reader.fieldnames is None:
        flash("CSV inválido (sem cabeçalho).", "err")
        return redirect(request.referrer or url_for("home"))

    original_fields = reader.fieldnames
    norm_fields = [norm_col(f) for f in original_fields]
    field_map = dict(zip(norm_fields, original_fields))

    # Esperado: data, lancamento, valor
    if "data" not in field_map or "lancamento" not in field_map or "valor" not in field_map:
        flash(f"CSV não reconhecido. Colunas encontradas: {norm_fields}", "err")
        return redirect(request.referrer or url_for("home"))

    col_date = field_map["data"]
    col_desc = field_map["lancamento"]
    col_value = field_map["valor"]

    conn = get_conn()
    cur = conn.cursor()

    imported, skipped = 0, 0

    for row in reader:
        desc = (row.get(col_desc) or "").strip()
        if not desc:
            skipped += 1
            continue

        try:
            d = parse_date_any(row.get(col_date))
        except Exception:
            skipped += 1
            continue

        cents_signed = parse_brl_to_cents_signed(row.get(col_value))
        if cents_signed == 0:
            skipped += 1
            continue

        # Compra geralmente positiva => Despesa; negativa => Receita (estorno/pagamento)
        ttype = "Receita" if cents_signed < 0 else "Despesa"

        cur.execute("""
            INSERT INTO transactions
            (competence_date,type,category,description,payment_method,amount_cents)
            VALUES (?,?,?,?,?,?)
        """, (d.isoformat(), ttype, "Outros", desc, "Cartão", abs(cents_signed)))

        imported += 1

    conn.commit()
    conn.close()

    flash(f"Importação cartão concluída ✅ ({imported} itens, {skipped} ignorados)", "ok")
    return redirect(url_for("month_view", ym=month_key(date.today())))


# init
init_db()

# opcional para rodar local (no Render o gunicorn usa app:app)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
