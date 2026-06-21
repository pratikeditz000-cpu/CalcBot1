"""
Production-Grade Telegram Calculator Bot
=========================================
Automatically detects and solves mathematical expressions in group chats.
Supports natural language math, scientific calculations, GST, EMI, unit
conversion, equation solving, and much more — all without commands.

Architecture:
  - Database       : SQLite via aiosqlite
  - Rate Limiter   : In-memory sliding window
  - Math Parser    : SymPy + numexpr (no eval, whitelist-only)
  - NLP Handler    : Regex-based pattern matching
  - Bot Framework  : python-telegram-bot v20+ (asyncio)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import numexpr as ne
import sympy
from dotenv import load_dotenv
from sympy import (
    E, oo, pi, symbols,
    Eq, solve,
    acos, asin, atan,
    cos, exp, factorial, log, sin, sqrt, tan,
    sympify,
)
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)
from telegram import (
    BotCommand,
    InlineQueryResultArticle,
    InputTextMessageContent,
    MessageEntity,
    Update,
)
from telegram.constants import MessageEntityType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("calcbot")

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
DB_PATH: str = os.getenv("DB_PATH", "calcbot.db")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Custom Telegram Premium emoji constants (ParseMode.HTML required)
# Use as: f"{E_CALC} result" or E_ERROR + " message"
E_CHART   = '<emoji id="6314203594203602602">📊</emoji>'
E_RECEIPT = '<emoji id="5444856076954520455">🧾</emoji>'
E_BANK    = '<emoji id="4994863664633741922">🏦</emoji>'
E_UP      = '<emoji id="5935913431801532272">📈</emoji>'
E_DOWN    = '<emoji id="5938539885907415367">📉</emoji>'
E_MONEY   = '<emoji id="6332536056516183967">💹</emoji>'
E_BDAY    = '<emoji id="5452055425690123301">🎂</emoji>'
E_THERMO  = '<emoji id="5192721971757993253">🌡️</emoji>'
E_RULER   = '<emoji id="6334362276610443521">📐</emoji>'
E_FX      = '<emoji id="6332536056516183967">💱</emoji>'
E_NUMS    = '<emoji id="6323436631428695574">🔢</emoji>'
E_BAG     = '<emoji id="6089104607328342288">💰</emoji>'
E_CALC    = '<emoji id="5265260005132608110">🧮</emoji>'
E_ERROR   = '<emoji id="5210952531676504517">❌</emoji>'
E_ROBOT   = '<emoji id="5327820601845369150">🤖</emoji>'
E_SEARCH  = '<emoji id="5888620056551625531">🔍</emoji>'
E_PING     = '<emoji id="5893203503915996356">⚡</emoji>'
E_VERIFIED = (
    '<emoji id="6206384886983429617">✅</emoji>'
    '<emoji id="6203758583201402540">✅</emoji>'
    '<emoji id="6204206093023841577">✅</emoji>'
    '<emoji id="6203877283212562092">✅</emoji>'
)
E_MUTE    = '<emoji id="6039505337151655702">🔇</emoji>'

MAX_MSG_LENGTH = 300          # Ignore messages longer than this
RATE_LIMIT_WINDOW = 60        # seconds
RATE_LIMIT_MAX = 15           # max requests per window per user
HISTORY_MAX = 50              # keep this many calls per group in memory

# Regex whitelist — characters allowed in raw expressions before parsing
EXPR_WHITELIST = re.compile(
    r"^[\d\s\+\-\*\/\^\(\)\.\,\!\%\=xXyYzZ"
    r"a-wA-W]+$"
)

# Dangerous patterns that must never reach the parser
BLOCKED_PATTERNS = re.compile(
    r"(import|exec|eval|__|\bos\b|\bsys\b|open\(|subprocess|lambda|class|def\s)",
    re.IGNORECASE,
)

# Minimum expression length to avoid false positives on short words
MIN_EXPR_LEN = 3

# Math function names allowed in expressions
ALLOWED_NAMES = {
    "sin": sympy.sin,
    "cos": sympy.cos,
    "tan": sympy.tan,
    "asin": sympy.asin,
    "acos": sympy.acos,
    "atan": sympy.atan,
    "sinh": sympy.sinh,
    "cosh": sympy.cosh,
    "tanh": sympy.tanh,
    "sqrt": sympy.sqrt,
    "log": sympy.log,
    "log2": lambda x: sympy.log(x, 2),
    "log10": lambda x: sympy.log(x, 10),
    "exp": sympy.exp,
    "abs": sympy.Abs,
    "factorial": sympy.factorial,
    "gcd": sympy.gcd,
    "lcm": sympy.lcm,
    "floor": sympy.floor,
    "ceiling": sympy.ceiling,
    "pi": sympy.pi,
    "e": sympy.E,
    "inf": sympy.oo,
}

# Sympy parser transformations (no implicit multiply to avoid false positives)
TRANSFORMATIONS = standard_transformations + (convert_xor,)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create tables if they do not exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id   INTEGER PRIMARY KEY,
                title      TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                joined_at  TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                calc_count INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL
            )
            """
        )
        # Seed counters
        for key in ("total_calculations", "total_users", "total_groups"):
            await db.execute(
                "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,)
            )
        await db.commit()


async def increment_stat(key: str, amount: int = 1) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stats SET value = value + ? WHERE key = ?", (amount, key)
        )
        await db.commit()


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM stats") as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT COUNT(*) FROM groups WHERE enabled = 1") as cur:
            (active_groups,) = await cur.fetchone()
        async with db.execute(
            "SELECT SUM(calc_count) FROM users"
        ) as cur:
            (user_calcs,) = await cur.fetchone()
    return {r[0]: r[1] for r in rows} | {"active_groups": active_groups, "user_calcs": user_calcs or 0}


async def upsert_user(user_id: int, username: Optional[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, calc_count, first_seen, last_seen)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                calc_count = calc_count + 1,
                last_seen  = excluded.last_seen
            """,
            (user_id, username, now, now),
        )
        await db.commit()


async def upsert_group(group_id: int, title: Optional[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO groups (group_id, title, enabled, joined_at)
            VALUES (?, ?, 1, ?)
            """,
            (group_id, title, now),
        )
        await db.execute(
            "UPDATE groups SET title = ? WHERE group_id = ?", (title, group_id)
        )
        await db.commit()


async def is_group_enabled(group_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled FROM groups WHERE group_id = ?", (group_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return True   # Default to enabled for new groups
    return bool(row[0])


async def set_group_enabled(group_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE groups SET enabled = ? WHERE group_id = ?",
            (int(enabled), group_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window rate limiter keyed by user_id."""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._records: dict[int, deque[float]] = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        dq = self._records[user_id]
        # Evict timestamps outside the window
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.max_calls:
            return False
        dq.append(now)
        return True

    def reset(self, user_id: int) -> None:
        self._records.pop(user_id, None)


rate_limiter = RateLimiter(RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)

# ---------------------------------------------------------------------------
# Input Sanitiser & Validator
# ---------------------------------------------------------------------------


def sanitise_input(text: str) -> Optional[str]:
    """
    Return a cleaned version of <b>text</b> if it looks like a safe math expression,
    otherwise return None.
    """
    if not text or len(text) > MAX_MSG_LENGTH:
        return None
    text = text.strip()
    if BLOCKED_PATTERNS.search(text):
        return None
    return text


def looks_like_expression(text: str) -> bool:
    """
    Heuristic check: does <b>text</b> contain enough math-like tokens to be worth
    trying to parse?  Avoids firing on normal prose.
    """
    # Must have at least one digit
    if not any(c.isdigit() for c in text):
        return False
    # Must have an operator OR a known function name
    has_op = bool(re.search(r"[\+\-\*\/\^\%\!]", text))
    has_fn = bool(re.search(
        r"\b(sin|cos|tan|sqrt|log|exp|abs|factorial|gcd|lcm|floor|ceiling|log2|log10|asin|acos|atan|sinh|cosh|tanh)\b",
        text, re.IGNORECASE,
    ))
    return has_op or has_fn


# ---------------------------------------------------------------------------
# Math Parser
# ---------------------------------------------------------------------------


def _preprocess(expr: str) -> str:
    """Normalise expression before passing to SymPy."""
    expr = expr.strip()
    # Replace ^ with ** for power
    expr = expr.replace("^", "**")
    # n! → factorial(n)
    expr = re.sub(r"(\d+)\!", r"factorial(\1)", expr)
    # Normalise whitespace
    expr = re.sub(r"\s+", " ", expr)
    return expr


def safe_eval(expr: str) -> Optional[str]:
    """
    Safely evaluate a mathematical expression using SymPy.
    Returns a string result or None on failure.
    NEVER uses eval().
    """
    try:
        expr = _preprocess(expr)
        result = parse_expr(expr, local_dict=ALLOWED_NAMES, transformations=TRANSFORMATIONS)
        numeric = result.evalf(10)

        # Format nicely
        if numeric.is_integer:
            val = int(numeric)
            return f"{val:,}"
        else:
            val = float(numeric)
            if abs(val) < 1e-10:
                return "0"
            # Avoid scientific notation for everyday numbers
            if 0.001 <= abs(val) <= 1e12:
                formatted = f"{val:,.6f}".rstrip("0").rstrip(".")
                return formatted
            else:
                return f"{val:.6e}"
    except Exception:
        return None


def safe_eval_fast(expr: str) -> Optional[str]:
    """
    Fast path using numexpr for simple arithmetic.
    Falls back to safe_eval (SymPy) on failure.
    """
    try:
        # numexpr does not understand factorial or sympy funcs
        # Only use it for pure arithmetic
        if re.search(r"[a-zA-Z]", expr):
            raise ValueError("Contains letters — skip numexpr")
        prepped = expr.replace("^", "**").replace("!", "")
        result = ne.evaluate(prepped)
        val = float(result)
        if val == int(val) and abs(val) < 1e15:
            return f"{int(val):,}"
        formatted = f"{val:,.6f}".rstrip("0").rstrip(".")
        return formatted
    except Exception:
        return safe_eval(expr)


# ---------------------------------------------------------------------------
# NLP Handler — Natural Language Math Patterns
# ---------------------------------------------------------------------------


def _fmt(value: float, precision: int = 2) -> str:
    """Format a float with thousand separators."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.{precision}f}"


class NLPHandler:
    """
    Pattern-based natural language math interpreter.
    Returns (matched: bool, result: str) tuples.
    """

    # -----------------------------------------------------------------------
    # Percentage
    # -----------------------------------------------------------------------

    @staticmethod
    def percentage_of(text: str) -> Optional[str]:
        """'25% of 4000'  or  '25 percent of 4000'"""
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:%|percent(?:age)?)\s+of\s+(\d+(?:[,\d]*(?:\.\d+)?)?)",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        pct = float(m.group(1))
        base = float(m.group(2).replace(",", ""))
        result = pct / 100 * base
        return (
            f"{E_CHART} <b>Percentage Calculation</b>\n"
            f"{_fmt(pct)}% of {_fmt(base)} = <b>{_fmt(result)}</b>"
        )

    # -----------------------------------------------------------------------
    # GST
    # -----------------------------------------------------------------------

    @staticmethod
    def gst(text: str) -> Optional[str]:
        """
        '2 lakh ka 18% GST'
        '200000 18% gst'
        '5000 + 12% gst'
        """
        m = re.search(
            r"(\d+(?:[,\d]*(?:\.\d+)?)?)\s*(?:lakh|lac)?\s*(?:ka|of|plus|\+)?\s*"
            r"(\d+(?:\.\d+)?)\s*%\s*(?:GST|gst|tax)",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        amount_raw = m.group(1).replace(",", "")
        amount = float(amount_raw)
        # Handle lakh/lac
        if re.search(r"lakh|lac", text, re.IGNORECASE):
            amount *= 100_000
        rate = float(m.group(2))
        gst_amount = amount * rate / 100
        total = amount + gst_amount
        return (
            f"{E_RECEIPT} <b>GST Calculation</b>\n"
            f"Amount        : ₹{_fmt(amount)}\n"
            f"GST Rate      : {rate}%\n"
            f"GST Amount    : ₹{_fmt(gst_amount)}\n"
            f"Total Amount  : ₹{_fmt(total)}"
        )

    # -----------------------------------------------------------------------
    # EMI
    # -----------------------------------------------------------------------

    @staticmethod
    def emi(text: str) -> Optional[str]:
        """
        '50000 loan 3 years 12%'
        'EMI for 500000 at 8.5% for 20 years'
        'loan 100000 5 year 10%'
        """
        m = re.search(
            r"(\d+(?:[,\d]*(?:\.\d+)?)?)\s*(?:loan|emi|at)?\s*"
            r"(?:at\s*)?(\d+(?:\.\d+)?)\s*%\s*(?:for\s*)?(\d+)\s*(?:year|yr|years|months?)?",
            text, re.IGNORECASE,
        )
        if not m:
            # Try alternate order: principal years rate
            m = re.search(
                r"(\d+(?:[,\d]*(?:\.\d+)?)?)\s+(?:loan|emi)?\s*(\d+)\s*(?:year|yr|years)\s*(\d+(?:\.\d+)?)\s*%",
                text, re.IGNORECASE,
            )
            if not m:
                return None
            principal = float(m.group(1).replace(",", ""))
            n_years = int(m.group(2))
            annual_rate = float(m.group(3))
        else:
            principal = float(m.group(1).replace(",", ""))
            annual_rate = float(m.group(2))
            n_years = int(m.group(3))

        # Monthly EMI formula: P * r * (1+r)^n / ((1+r)^n - 1)
        monthly_rate = annual_rate / 12 / 100
        n_months = n_years * 12
        if monthly_rate == 0:
            emi = principal / n_months
        else:
            emi = principal * monthly_rate * (1 + monthly_rate) ** n_months / (
                (1 + monthly_rate) ** n_months - 1
            )
        total_payment = emi * n_months
        total_interest = total_payment - principal
        return (
            f"{E_BANK} <b>EMI Calculation</b>\n"
            f"Principal     : ₹{_fmt(principal)}\n"
            f"Annual Rate   : {annual_rate}%\n"
            f"Tenure        : {n_years} years ({n_months} months)\n"
            f"Monthly EMI   : ₹{_fmt(emi)}\n"
            f"Total Payment : ₹{_fmt(total_payment)}\n"
            f"Total Interest: ₹{_fmt(total_interest)}"
        )

    # -----------------------------------------------------------------------
    # Profit / Loss
    # -----------------------------------------------------------------------

    @staticmethod
    def profit_loss(text: str) -> Optional[str]:
        """
        'profit on cp 500 sp 750'
        'loss cp 1000 sp 800'
        'bought 500 sold 620'
        """
        m = re.search(
            r"(?:cp|cost\s<b>price|bought)\s</b>(?:=|:)?\s*(\d+(?:\.\d+)?)"
            r".*?(?:sp|sell(?:ing)?\s<b>price|sold)\s</b>(?:=|:)?\s*(\d+(?:\.\d+)?)",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        cp = float(m.group(1))
        sp = float(m.group(2))
        diff = sp - cp
        pct = abs(diff) / cp * 100
        label = "Profit" if diff >= 0 else "Loss"
        emoji = f"{E_UP}" if diff >= 0 else f"{E_DOWN}"
        return (
            f"{emoji} <b>{label} Calculation</b>\n"
            f"Cost Price    : ₹{_fmt(cp)}\n"
            f"Selling Price : ₹{_fmt(sp)}\n"
            f"{label}         : ₹{_fmt(abs(diff))}\n"
            f"{label} %       : {_fmt(pct)}%"
        )

    # -----------------------------------------------------------------------
    # Compound Interest
    # -----------------------------------------------------------------------

    @staticmethod
    def compound_interest(text: str) -> Optional[str]:
        """
        'ci 10000 5 years 8%'
        'compound interest 50000 3 years 12%'
        """
        m = re.search(
            r"(?:ci|compound\s<b>interest)\s+(\d+(?:[,\d]</b>(?:\.\d+)?)?)\s+"
            r"(\d+)\s*(?:year|yr|years)\s+(\d+(?:\.\d+)?)\s*%",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        principal = float(m.group(1).replace(",", ""))
        years = int(m.group(2))
        rate = float(m.group(3))
        amount = principal * (1 + rate / 100) ** years
        ci = amount - principal
        return (
            f"{E_MONEY} <b>Compound Interest</b>\n"
            f"Principal     : ₹{_fmt(principal)}\n"
            f"Rate          : {rate}% per annum\n"
            f"Time          : {years} years\n"
            f"Interest (CI) : ₹{_fmt(ci)}\n"
            f"Total Amount  : ₹{_fmt(amount)}"
        )

    # -----------------------------------------------------------------------
    # Age Calculation
    # -----------------------------------------------------------------------

    @staticmethod
    def age_calc(text: str) -> Optional[str]:
        """
        'age born 1990'
        'born in 1995'
        'age of person born 15 march 1988'
        """
        m = re.search(
            r"(?:age|born)\s+(?:in\s+|of\s+)?(?:\d{1,2}\s+\w+\s+)?(\d{4})",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        birth_year = int(m.group(1))
        current_year = datetime.now().year
        if birth_year > current_year or birth_year < 1900:
            return None
        age = current_year - birth_year
        return (
            f"{E_BDAY} <b>Age Calculation</b>\n"
            f"Birth Year    : {birth_year}\n"
            f"Current Year  : {current_year}\n"
            f"Approximate Age: <b>{age} years</b>"
        )

    # -----------------------------------------------------------------------
    # Unit Conversion
    # -----------------------------------------------------------------------

    UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
        # Length
        ("km", "m"): 1_000,
        ("m", "km"): 0.001,
        ("m", "cm"): 100,
        ("cm", "m"): 0.01,
        ("m", "mm"): 1_000,
        ("mm", "m"): 0.001,
        ("km", "miles"): 0.621371,
        ("miles", "km"): 1.60934,
        ("foot", "m"): 0.3048,
        ("feet", "m"): 0.3048,
        ("m", "feet"): 3.28084,
        ("inch", "cm"): 2.54,
        ("cm", "inch"): 0.393701,
        ("yard", "m"): 0.9144,
        ("m", "yard"): 1.09361,
        # Weight / Mass
        ("kg", "g"): 1_000,
        ("g", "kg"): 0.001,
        ("kg", "lb"): 2.20462,
        ("lb", "kg"): 0.453592,
        ("kg", "oz"): 35.274,
        ("oz", "kg"): 0.0283495,
        ("tonne", "kg"): 1_000,
        ("kg", "tonne"): 0.001,
        # Temperature handled separately
        # Area
        ("km2", "m2"): 1e6,
        ("m2", "km2"): 1e-6,
        ("hectare", "m2"): 10_000,
        ("m2", "hectare"): 0.0001,
        ("acre", "m2"): 4046.86,
        # Volume
        ("litre", "ml"): 1_000,
        ("ml", "litre"): 0.001,
        ("litre", "gallon"): 0.264172,
        ("gallon", "litre"): 3.78541,
        # Speed
        ("kmh", "ms"): 0.277778,
        ("ms", "kmh"): 3.6,
        ("mph", "kmh"): 1.60934,
        ("kmh", "mph"): 0.621371,
        # Data
        ("gb", "mb"): 1_024,
        ("mb", "gb"): 1 / 1024,
        ("tb", "gb"): 1_024,
        ("gb", "tb"): 1 / 1024,
        ("mb", "kb"): 1_024,
        ("kb", "mb"): 1 / 1024,
    }

    @classmethod
    def unit_convert(cls, text: str) -> Optional[str]:
        """'convert 5 km to m'  or  '100 kg in lb'"""
        m = re.search(
            r"(?:convert\s+)?(\d+(?:\.\d+)?)\s+(\w+(?:\d+)?)\s+(?:to|in|into)\s+(\w+(?:\d+)?)",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        value = float(m.group(1))
        from_unit = m.group(2).lower()
        to_unit = m.group(3).lower()

        # Temperature special case
        if from_unit in ("c", "celsius") and to_unit in ("f", "fahrenheit"):
            result = value * 9 / 5 + 32
            return f"{E_THERMO} <b>Temperature</b> : {_fmt(value)}°C = <b>{_fmt(result)}°F</b>"
        if from_unit in ("f", "fahrenheit") and to_unit in ("c", "celsius"):
            result = (value - 32) * 5 / 9
            return f"{E_THERMO} <b>Temperature</b> : {_fmt(value)}°F = <b>{_fmt(result)}°C</b>"
        if from_unit in ("c", "celsius") and to_unit in ("k", "kelvin"):
            result = value + 273.15
            return f"{E_THERMO} <b>Temperature</b> : {_fmt(value)}°C = <b>{_fmt(result)} K</b>"
        if from_unit in ("k", "kelvin") and to_unit in ("c", "celsius"):
            result = value - 273.15
            return f"{E_THERMO} <b>Temperature</b> : {_fmt(value)} K = <b>{_fmt(result)}°C</b>"

        factor = cls.UNIT_CONVERSIONS.get((from_unit, to_unit))
        if factor is None:
            return None
        result = value * factor
        return (
            f"{E_RULER} <b>Unit Conversion</b>\n"
            f"{_fmt(value)} {m.group(2)} = <b>{_fmt(result)} {m.group(3)}</b>"
        )

    # -----------------------------------------------------------------------
    # Currency Conversion (static rates — for live rates add an API call)
    # -----------------------------------------------------------------------

    CURRENCY_RATES_TO_INR: dict[str, float] = {
        "usd": 83.50,
        "eur": 90.20,
        "gbp": 105.40,
        "jpy": 0.56,
        "cad": 61.20,
        "aud": 54.30,
        "sgd": 61.80,
        "aed": 22.72,
        "chf": 93.10,
        "inr": 1.0,
    }

    @classmethod
    def currency(cls, text: str) -> Optional[str]:
        """'100 USD to INR'  or  '500 EUR in USD'"""
        m = re.search(
            r"(\d+(?:\.\d+)?)\s+([A-Z]{3})\s+(?:to|in|into)\s+([A-Z]{3})",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        amount = float(m.group(1))
        frm = m.group(2).lower()
        to = m.group(3).lower()
        rate_from = cls.CURRENCY_RATES_TO_INR.get(frm)
        rate_to = cls.CURRENCY_RATES_TO_INR.get(to)
        if rate_from is None or rate_to is None:
            return None
        inr_equivalent = amount * rate_from
        result = inr_equivalent / rate_to
        return (
            f"{E_FX} <b>Currency Conversion</b> <i>(static rates)</i>\n"
            f"{_fmt(amount)} {m.group(2).upper()} = <b>{_fmt(result)} {m.group(3).upper()}</b>"
        )

    # -----------------------------------------------------------------------
    # Equation Solving
    # -----------------------------------------------------------------------

    @staticmethod
    def solve_equation(text: str) -> Optional[str]:
        """
        'solve x^2 + 5x + 6 = 0'
        'solve 2x + 3 = 7'
        """
        m = re.search(
            r"solve\s+(.+?)\s*=\s*(.+)",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        lhs_str = m.group(1).strip().replace("^", "**")
        rhs_str = m.group(2).strip().replace("^", "**")

        # Block dangerous input
        if BLOCKED_PATTERNS.search(lhs_str) or BLOCKED_PATTERNS.search(rhs_str):
            return None

        try:
            x = symbols("x")
            safe_dict = {**ALLOWED_NAMES, "x": x}
            lhs = parse_expr(lhs_str, local_dict=safe_dict, transformations=TRANSFORMATIONS)
            rhs = parse_expr(rhs_str, local_dict=safe_dict, transformations=TRANSFORMATIONS)
            equation = Eq(lhs, rhs)
            solutions = solve(equation, x)
            if not solutions:
                return f"{E_NUMS} <b>Equation Solver</b>\nNo real solutions found."
            sol_str = ", ".join(str(s) for s in solutions)
            return f"{E_NUMS} <b>Equation Solver</b>\n<code>{m.group(1)} = {m.group(2)}</code>\nx = <b>{sol_str}</b>"
        except Exception as exc:
            logger.debug("Equation solve error: %s", exc)
            return None

    # -----------------------------------------------------------------------
    # Lakh / Crore shorthand
    # -----------------------------------------------------------------------

    @staticmethod
    def lakh_crore(text: str) -> Optional[str]:
        """
        '2 lakh' → 200000
        '1.5 crore' → 15000000
        '5 lakh + 3 crore'
        """
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*(lakh|lac|crore)\b",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit in ("lakh", "lac"):
            result = value * 100_000
        else:
            result = value * 10_000_000
        return (
            f"{E_BAG} <b>Indian Numeral</b>\n"
            f"{_fmt(value)} {m.group(2)} = <b>{_fmt(result)}</b>"
        )

    # -----------------------------------------------------------------------
    # Runner — try all patterns in order
    # -----------------------------------------------------------------------

    @classmethod
    def try_all(cls, text: str) -> Optional[str]:
        """Try every NLP handler; return the first match."""
        handlers = [
            cls.solve_equation,
            cls.gst,
            cls.emi,
            cls.compound_interest,
            cls.profit_loss,
            cls.percentage_of,
            cls.unit_convert,
            cls.currency,
            cls.age_calc,
            cls.lakh_crore,
        ]
        for handler in handlers:
            try:
                result = handler(text)
                if result:
                    return result
            except Exception as exc:
                logger.debug("NLP handler %s error: %s", handler.__name__, exc)
        return None


nlp = NLPHandler()

# ---------------------------------------------------------------------------
# Enabled-group cache (avoid DB hit on every message)
# ---------------------------------------------------------------------------


class GroupCache:
    """Simple TTL cache for group enabled/disabled status."""

    TTL = 300  # seconds

    def __init__(self) -> None:
        self._data: dict[int, tuple[bool, float]] = {}

    async def get(self, group_id: int) -> bool:
        entry = self._data.get(group_id)
        if entry and time.monotonic() - entry[1] < self.TTL:
            return entry[0]
        enabled = await is_group_enabled(group_id)
        self._data[group_id] = (enabled, time.monotonic())
        return enabled

    def set(self, group_id: int, enabled: bool) -> None:
        self._data[group_id] = (enabled, time.monotonic())


group_cache = GroupCache()

# ---------------------------------------------------------------------------
# Core Message Handler
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Main handler: fires on every text message in a group.
    Detects mathematical expressions or NLP math patterns and replies.
    Messages starting with '=' are prioritised — no heuristics, instant eval.
    """
    message = update.effective_message
    if message is None:
        return

    # --- Ignore non-text, forwards, and messages from bots ---
    if not message.text:
        return
    if message.forward_origin is not None:
        return
    if update.effective_user and update.effective_user.is_bot:
        return

    text = message.text.strip()

    # --- Length guard ---
    if len(text) > MAX_MSG_LENGTH:
        return

    # --- Must be in a group/supergroup ---
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return

    # --- Check group enabled status ---
    if not await group_cache.get(chat.id):
        return

    user = update.effective_user
    if user is None:
        return

    # --- Rate limiting ---
    if not rate_limiter.is_allowed(user.id):
        logger.debug("Rate-limited user %d", user.id)
        return

    # =========================================================
    # PRIORITY PATH: '=' prefix — skip ALL heuristics, eval now
    # =========================================================
    if text.startswith("="):
        expr = text[1:].strip()
        if not expr:
            return
        clean = sanitise_input(expr)
        if clean is None:
            return
        result = safe_eval_fast(clean)
        if result:
            await _send_reply(message, f"{E_CALC} <code>{clean}</code> = <b>{result}</b>", is_formatted=True)
            await _record_usage(user, chat)
        else:
            await _send_reply(message, f"{E_ERROR} Could not evaluate that expression.", is_formatted=False)
        return

    # --- Sanitise for standard path ---
    clean = sanitise_input(text)
    if clean is None:
        return

    # === Step 1: Try NLP patterns first (natural language) ===
    nlp_result = nlp.try_all(clean)
    if nlp_result:
        await _send_reply(message, nlp_result, is_formatted=True)
        await _record_usage(user, chat)
        return

    # === Step 2: Try pure math expression ===
    if not looks_like_expression(clean):
        return

    result = safe_eval_fast(clean)
    if result is None:
        return

    response = f"{E_CALC} <code>{clean}</code> = <b>{result}</b>"
    await _send_reply(message, response, is_formatted=True)
    await _record_usage(user, chat)



def _build_entities(tagged: str) -> tuple[str, list]:
    """
    Parse a string containing <b>, <i>, <code>, <emoji id="..."> tags
    into (plain_text, [MessageEntity]) for Telegram Bot API.
    UTF-16 offsets are used as required by the API.
    """
    def u16(s: str) -> int:
        return len(s.encode('utf-16-le')) // 2

    TOKEN = re.compile(
        r'<b>(.*?)</b>'
        r'|<i>(.*?)</i>'
        r'|<code>(.*?)</code>'
        r'|<emoji id="([^"]+)">(.*?)</emoji>'
        r'|([^<]+)'       # plain text (no '<')
        r'|(<)',           # lone '<' pass-through
        re.DOTALL,
    )
    plain = ''
    ents: list = []
    for m in TOKEN.finditer(tagged):
        if m.group(1) is not None:          # <b>
            c = m.group(1)
            ents.append(MessageEntity(type=MessageEntityType.BOLD,   offset=u16(plain), length=u16(c)))
            plain += c
        elif m.group(2) is not None:        # <i>
            c = m.group(2)
            ents.append(MessageEntity(type=MessageEntityType.ITALIC, offset=u16(plain), length=u16(c)))
            plain += c
        elif m.group(3) is not None:        # <code>
            c = m.group(3)
            ents.append(MessageEntity(type=MessageEntityType.CODE,   offset=u16(plain), length=u16(c)))
            plain += c
        elif m.group(4) is not None:        # <emoji id="...">
            eid, echar = m.group(4), m.group(5)
            ents.append(MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=u16(plain),
                length=u16(echar),
                custom_emoji_id=eid,
            ))
            plain += echar
        else:
            plain += (m.group(6) or '') + (m.group(7) or '')
    return plain, ents


def _itmc(tagged: str) -> InputTextMessageContent:
    """Inline text message content with proper custom-emoji entities."""
    plain, ents = _build_entities(tagged)
    return InputTextMessageContent(plain, entities=ents or None)


async def _send_reply(message, text: str, is_formatted: bool = False) -> None:
    """Send a reply, handling Telegram API exceptions gracefully."""
    try:
        plain, ents = _build_entities(text)
        await message.reply_text(
            plain,
            entities=ents or None,
            quote=True,
        )
    except Exception as exc:
        logger.warning("Failed to send reply: %s", exc)


async def _record_usage(user, chat) -> None:
    """Async fire-and-forget DB writes."""
    try:
        await asyncio.gather(
            increment_stat("total_calculations"),
            upsert_user(user.id, user.username),
            upsert_group(chat.id, getattr(chat, "title", None)),
        )
    except Exception as exc:
        logger.warning("DB write failed: %s", exc)


# ---------------------------------------------------------------------------
# Private / DM Handler
# ---------------------------------------------------------------------------


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Allow the bot to work in private chats too — useful for testing.
    Messages starting with '=' are prioritised — instant eval, no heuristics.
    """
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text.startswith("/"):
        return   # Let command handlers deal with it

    if len(text) > MAX_MSG_LENGTH:
        return

    user = update.effective_user
    if user and not rate_limiter.is_allowed(user.id):
        await message.reply_text("⏳ Slow down! You're sending too many requests.")
        return

    # =========================================================
    # PRIORITY PATH: '=' prefix — skip ALL heuristics, eval now
    # =========================================================
    if text.startswith("="):
        expr = text[1:].strip()
        if not expr:
            return
        clean = sanitise_input(expr)
        if clean is None:
            await _send_reply(message, f"{E_ERROR} Expression blocked for security reasons.")
            return
        result = safe_eval_fast(clean)
        if result:
            await _send_reply(message, f"{E_CALC} <code>{clean}</code> = <b>{result}</b>", is_formatted=True)
        else:
            await _send_reply(message, f"{E_ERROR} Couldn't evaluate that expression. Check the syntax.")
        return

    clean = sanitise_input(text)
    if clean is None:
        return

    nlp_result = nlp.try_all(clean)
    if nlp_result:
        await _send_reply(message, nlp_result, is_formatted=True)
        return

    if not looks_like_expression(clean):
        await _send_reply(
            message,
            "Send me a math expression like:\n"
            "<code>=2+2</code> · <code>=sqrt(144)</code> · <code>=(50000*18)/100</code>\n\n"
            "Or natural language:\n"
            "<code>25% of 4000</code> · <code>50000 loan 3 years 12%</code>\n"
            "<code>solve x^2 + 5x + 6 = 0</code> · <code>convert 5 km to m</code>\n\n"
            "Tip: Start any expression with <code>=</code> for instant calculation!",
        )
        return

    result = safe_eval_fast(clean)
    if result:
        await _send_reply(message, f"{E_CALC} <code>{clean}</code> = <b>{result}</b>", is_formatted=True)
    else:
        await _send_reply(message, f"{E_ERROR} Couldn't evaluate that expression. Please check the syntax.")


# ---------------------------------------------------------------------------
# Inline Query Handler
# ---------------------------------------------------------------------------


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Inline mode: user types @botusername <expression> in ANY chat.
    Bot shows a result card — clicking it sends the full calculation to that chat.

    Enable inline mode via @BotFather → /setinline → set placeholder text.
    """
    query = update.inline_query
    if query is None:
        return

    raw = query.query.strip()

    # Show a hint card when the field is empty
    if not raw:
        hint = InlineQueryResultArticle(
            id="hint",
            title="Type a calculation…",
            description="e.g.  2+2   •   sqrt(144)   •   25% of 4000   •   50000 loan 3y 12%",
            input_message_content=InputTextMessageContent(
                "ℹ️ Type an expression after the bot username to calculate it inline.",
            ),
            thumbnail_url="https://i.imgur.com/4M34hi2.png",
        )
        await query.answer([hint], cache_time=0)
        return

    if len(raw) > MAX_MSG_LENGTH:
        return

    if BLOCKED_PATTERNS.search(raw):
        return

    results: list[InlineQueryResultArticle] = []

    # --- Try NLP patterns ---
    nlp_result = nlp.try_all(raw)
    if nlp_result:
        # Strip markdown for the description preview (plain text)
        plain = re.sub(r'<[^>]+>', '', nlp_result)
        results.append(
            InlineQueryResultArticle(
                id="nlp",
                title=plain.split("\n")[0],
                description="\n".join(plain.split("\n")[1:])[:120] or raw,
                input_message_content=_itmc(nlp_result),
            )
        )

    # --- Try math expression (with or without '=' prefix) ---
    expr = raw.lstrip("=").strip()
    clean = sanitise_input(expr)
    if clean and (looks_like_expression(clean) or raw.startswith("=")):
        math_result = safe_eval_fast(clean)
        if math_result and not any(r.id == "nlp" for r in results):
            results.append(
                InlineQueryResultArticle(
                    id="math",
                    title=f"{clean} = {math_result}",
                    description=f"Result: {math_result}",
                    input_message_content=_itmc(f"{E_CALC} <code>{clean}</code> = <b>{math_result}</b>"),
                )
            )
        elif math_result:
            # Append alongside NLP if both matched
            results.append(
                InlineQueryResultArticle(
                    id="math",
                    title=f"= {math_result}",
                    description=f"{clean} = {math_result}",
                    input_message_content=_itmc(f"{E_CALC} <code>{clean}</code> = <b>{math_result}</b>"),
                )
            )

    if results:
        await query.answer(results, cache_time=10)
    else:
        no_match = InlineQueryResultArticle(
            id="nomatch",
            title="No result",
            description=f"Could not evaluate: {raw[:60]}",
            input_message_content=_itmc(f"{E_ERROR} Could not evaluate: <code>{raw[:60]}</code>"),
        )
        await query.answer([no_match], cache_time=5)


# ---------------------------------------------------------------------------
# Admin Commands
# ---------------------------------------------------------------------------


def _is_admin(update: Update) -> bool:
    """Check if user is a Telegram admin (via ADMIN_IDS env var)."""
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    if not admin_ids_raw:
        return True   # If no admin list set, allow all
    admin_ids = {int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()}
    user = update.effective_user
    return user is not None and user.id in admin_ids


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help message."""
    msg = update.effective_message
    if msg is None:
        return
    help_text = (
        f"{E_ROBOT} <b>Calculator Bot Help</b>\n\n"
        "I automatically solve math in group chats — no commands needed!\n\n"
        f"{E_PING} <b>Instant Mode (= prefix)</b>\n"
        "Start with <code>=</code> to skip detection and calculate instantly:\n"
        "<code>=2+2</code> · <code>=sqrt(144)</code> · <code>=(50000*18)/100</code>\n\n"
        f"{E_SEARCH} <b>Inline Mode</b>\n"
        "Use me in ANY chat without adding me to it:\n"
        "Type <code>@YourBotUsername 2+2</code> in the message box\n"
        "→ tap the result card to send it\n\n"
        "<b>Basic Math:</b>\n"
        "<code>2+2</code> · <code>15000*18/100</code> · <code>(10+20)*5</code>\n\n"
        "<b>Scientific:</b>\n"
        "<code>sqrt(144)</code> · <code>sin(90)</code> · <code>log(100)</code> · <code>5!</code>\n\n"
        "<b>Natural Language:</b>\n"
        "<code>25% of 4000</code>\n"
        "<code>2 lakh ka 18% GST</code>\n"
        "<code>50000 loan 3 years 12%</code>\n"
        "<code>CI 10000 5 years 8%</code>\n"
        "<code>cp 500 sp 750</code>\n"
        "<code>age born 1990</code>\n"
        "<code>convert 5 km to m</code>\n"
        "<code>100 USD to INR</code>\n"
        "<code>solve x^2 + 5x + 6 = 0</code>\n\n"
        "<b>Admin Commands:</b>\n"
        "<code>/stats</code> <code>/enable</code> <code>/disable</code> <code>/ping</code>\n\n"
        "Rate limit: 15 calculations per minute per user."
    )
    await _send_reply(msg, help_text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show usage statistics (admin only)."""
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(update):
        await _send_reply(msg, f"{E_ERROR} Admin only command.")
        return
    try:
        stats = await get_stats()
        text = (
            f"{E_CHART} <b>Bot Statistics</b>\n\n"
            f"Total Calculations : {stats.get('total_calculations', 0):,}\n"
            f"Total Users        : {stats.get('total_users', 0):,}\n"
            f"Total Groups       : {stats.get('total_groups', 0):,}\n"
            f"Active Groups      : {stats.get('active_groups', 0):,}\n"
        )
        await _send_reply(msg, text)
    except Exception as exc:
        logger.error("Stats error: %s", exc)
        await _send_reply(msg, f"{E_ERROR} Error fetching stats.")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health check."""
    msg = update.effective_message
    if msg is None:
        return
    start = time.monotonic()
    _p, _e = _build_entities(f"{E_PING} Pong!")
    sent = await msg.reply_text(_p, entities=_e or None)
    latency = (time.monotonic() - start) * 1000
    _p2, _e2 = _build_entities(f"{E_PING} Pong! <i>{latency:.0f} ms</i>")
    await sent.edit_text(_p2, entities=_e2 or None)


async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable the bot in this group."""
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(update):
        await _send_reply(msg, f"{E_ERROR} Admin only command.")
        return
    chat = update.effective_chat
    if chat is None:
        return
    await set_group_enabled(chat.id, True)
    group_cache.set(chat.id, True)
    await _send_reply(msg, f"{E_VERIFIED} Bot <b>enabled</b> in this group.")


async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable the bot in this group (silently ignores all messages)."""
    msg = update.effective_message
    if msg is None:
        return
    if not _is_admin(update):
        await _send_reply(msg, f"{E_ERROR} Admin only command.")
        return
    chat = update.effective_chat
    if chat is None:
        return
    await set_group_enabled(chat.id, False)
    group_cache.set(chat.id, False)
    await _send_reply(msg, f"{E_MUTE} Bot <b>disabled</b> in this group.")


# ---------------------------------------------------------------------------
# Error Handler
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused exception", exc_info=context.error)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    """Runs once when the bot starts."""
    await init_db()
    # Register the / command menu visible in every Telegram client
    await application.bot.set_my_commands([
        BotCommand("start",   "Welcome message & usage guide"),
        BotCommand("help",    "All features, examples & tips"),
        BotCommand("stats",   "Bot usage statistics (admin only)"),
        BotCommand("ping",    "Check bot response latency"),
        BotCommand("enable",  "Enable bot in this group (admin only)"),
        BotCommand("disable", "Mute bot in this group (admin only)"),
    ])
    logger.info("Database initialised — bot is ready.")


def main() -> None:
    logger.info("Starting Calculator Bot…")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)   # Process multiple updates simultaneously
        .build()
    )

    # Admin commands
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("start",   cmd_help))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(CommandHandler("enable",  cmd_enable))
    app.add_handler(CommandHandler("disable", cmd_disable))

    # Group message handler — all non-command text
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handle_message,
        )
    )

    # Private message handler
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private,
        )
    )

    # Inline query handler (requires /setinline via @BotFather)
    app.add_handler(InlineQueryHandler(handle_inline_query))

    app.add_error_handler(error_handler)

    logger.info("Bot polling — press Ctrl+C to stop.")
    app.run_polling(
        allowed_updates=["message", "edited_message", "inline_query"],
        drop_pending_updates=True,   # Ignore backlog on startup
    )


if __name__ == "__main__":
    main()
