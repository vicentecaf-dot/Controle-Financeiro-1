import unicodedata
import pandas as pd
import io
import re
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

def norm_col(s: str) -> str:
    # remove acentos e padroniza
    s = str(s).strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = s.replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    return s

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

def parse_brl_to_cents(x) -> int:
    """
    Aceita: -100,00 | 17.385,28 | 9.99 | -2741.88 | "R$ -54,00"
    Retorna centavos (int) com sinal.
    """
    if x is None:
        return 0
    s = str(x).strip()
    s = s.replace("R$", "").replace(" ", "")

    # Se vier como "17.385,28" (pt-BR)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    # Se vier como "100,00"
    elif "," in s and "." not in s:
        s = s.replace(",", ".")

    # remove qualquer coisa que não seja número, sinal, ponto
    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0

    val = float(s)
    return int(round(val * 100))


def parse_date_flexible(x) -> date:
    """
    Aceita: 31/01/2026, 2026-02-05, datetime
    """
    if isinstance(x, (datetime, date)):
        return x if isinstance(x, date) else x.date()
    s = str(x).strip()
    # tenta dd/mm/yyyy
    if "/" in s:
        return datetime.strptime(s, "%d/%m/%Y").date()
    # tenta yyyy-mm-dd
    return datetime.strptime(s, "%Y-%m-%d").date()


def infer_method_from_desc(desc: str) -> str:
    d = (desc or "").upper()
    if "PIX" in d:
        return "PIX"
    if "TED" in d or "DOC" in d or "TRANSF" in d or "TRANSFER" in d:
        return "Transferência"
    if "BOLETO" in d:
        return "Boleto"
    # débito/compra em conta varia muito; deixo como Débito por padrão
    return "Débito"

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
df.columns = [norm_col(c) for c in df.columns]
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
@app.route("/import", methods=["POST"])
def import_file():
    file = request.files.get("file")
    source = request.form.get("source")  # "conta" ou "cartao"

    if not file or file.filename == "":
        flash("Nenhum arquivo enviado.", "err")
        return redirect(request.referrer or url_for("home"))

    filename = file.filename.lower()

    # Lê arquivo
    try:
        if filename.endswith(".csv"):
            # detecta separador automaticamente (vírgula / ponto e vírgula)
            raw = file.read()
            df = pd.read_csv(io.BytesIO(raw), sep=None, engine="python")
        else:
            df = pd.read_excel(file)
    except Exception as e:
        flash(f"Erro ao ler arquivo: {e}", "err")
        return redirect(request.referrer or url_for("home"))

    conn = get_conn()
    cur = conn.cursor()

    imported = 0
    skipped = 0

    try:
        if source == "conta":
            # Espera algo como: data | lançamento | valor (R$) | ...
            cols = [c.strip().lower() for c in df.columns]
            df.columns = cols
col_date = "data"
col_desc = "lancamento"   # após normalização, "lançamento" vira "lancamento"
col_value = "valor (r$)" if "valor (r$)" in df.columns else "valor"
            # tenta achar nomes parecidos
            col_date = "data"
            col_desc = "lançamento" if "lançamento" in cols else "lancamento"
            col_value = "valor (r$)" if "valor (r$)" in cols else "valor"

            if col_date not in cols or col_desc not in cols or col_value not in cols:
                flash("Não encontrei colunas esperadas no extrato da conta (data, lançamento, valor).", "err")
                conn.close()
                return redirect(url_for("home"))

            for _, r in df.iterrows():
                desc = str(r.get(col_desc, "")).strip()

                # pula saldo anterior e linhas vazias
                if not desc or "SALDO ANTERIOR" in desc.upper():
                    skipped += 1
                    continue

                d = parse_date_flexible(r.get(col_date))
                cents = parse_brl_to_cents(r.get(col_value))

                if cents == 0:
                    skipped += 1
                    continue

                ttype = "Receita" if cents > 0 else "Despesa"
                method = infer_method_from_desc(desc)
                category = "Outros"

                cur.execute("""
                    INSERT INTO transactions(competence_date, type, category, description, payment_method, amount_cents)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (d.isoformat(), ttype, category, desc, method, abs(cents)))

                imported += 1

            conn.commit()
            conn.close()
            flash(f"Importação conta concluída ✅ ({imported} lançamentos, {skipped} ignorados)", "ok")
            return redirect(url_for("month_view", ym=month_key(date.today())))

        elif source == "cartao":
            # Espera colunas: data, lançamento, valor (ou exatamente data,lançamento,valor)
            cols = [c.strip().lower() for c in df.columns]
            df.columns = cols

            col_date = "data"
            col_desc = "lançamento" if "lançamento" in cols else "lancamento"
            col_value = "valor"

            if col_date not in cols or col_desc not in cols or col_value not in cols:
                flash("Não encontrei colunas esperadas na fatura do cartão (data, lançamento, valor).", "err")
                conn.close()
                return redirect(url_for("home"))

            for _, r in df.iterrows():
                desc = str(r.get(col_desc, "")).strip()
                if not desc:
                    skipped += 1
                    continue

                d = parse_date_flexible(r.get(col_date))
                cents = parse_brl_to_cents(r.get(col_value))

                if cents == 0:
                    skipped += 1
                    continue

                # na fatura, geralmente compra vem positiva; pagamento pode vir negativo.
                # regra: se for negativo, tratamos como Receita (pagamento/estorno). Se positivo, Despesa.
                ttype = "Receita" if cents < 0 else "Despesa"

                method = "Cartão"
                category = "Outros"

                cur.execute("""
                    INSERT INTO transactions(competence_date, type, category, description, payment_method, amount_cents)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (d.isoformat(), ttype, category, desc, method, abs(cents)))

                imported += 1

            conn.commit()
            conn.close()
            flash(f"Importação cartão concluída ✅ ({imported} lançamentos, {skipped} ignorados)", "ok")
            return redirect(url_for("month_view", ym=month_key(date.today())))

        else:
            conn.close()
            flash("Selecione a origem correta (Conta ou Cartão).", "err")
            return redirect(url_for("home"))

    except Exception as e:
        conn.rollback()
        conn.close()
        flash(f"Erro importando: {e}", "err")
        return redirect(url_for("home"))

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
