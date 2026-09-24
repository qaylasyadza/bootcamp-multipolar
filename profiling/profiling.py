import os
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from beanie import Document, init_beanie
from pymongo import AsyncMongoClient

# TASK 1 POIN 1: "setup service baru profiling.py"
# -> Ini FastAPI app TERPISAH dari main.py (servis sendiri, port sendiri, image sendiri), tapi baca database MongoDB yang SAMA dengan main.py -- collection trx_collection.
# -> Env var (MONGODB_URI, MONGODB_DB_NAME) dan pola startup-nya dibuat identik dengan main.py, supaya konsisten satu sama lain.

load_dotenv()

app = FastAPI()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "bootcamp")

# Parameter moving average
MOVING_AVERAGE_WINDOW_MONTHS = 3
ALERT_THRESHOLD_RATIO = 1


class TrxType(str, Enum):
    income = "income"
    purchase = "purchase"


class PaymentMethod(str, Enum):
    cash        = "cash"
    gopay       = "gopay"
    ovo         = "ovo"
    shopee      = "shopee"
    bni         = "bni"
    bca         = "bca"
    bri         = "bri"
    mandiri     = "mandiri"
    dana        = "dana"

class Transaction(Document):
    date: datetime
    amount: int
    method: PaymentMethod
    desc: str
    trx_type: TrxType

    class Settings:
        name = "ferdi_final"


@app.on_event("startup")
async def init_db():
    if not MONGODB_URI:
        raise RuntimeError(
            "Environment variable MONGODB_URI belum diset. "
            "profiling.py butuh koneksi ke database yang SAMA dengan main.py."
        )
    client = AsyncMongoClient(MONGODB_URI)
    await init_beanie(database=client[MONGODB_DB_NAME], document_models=[Transaction])


def _month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    """Geser (year, month) mundur (delta negatif) atau maju sejumlah bulan."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


async def _get_monthly_purchase_total(year: int, month: int) -> int:
    start, end = _month_bounds(year, month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end}, "trx_type": TrxType.purchase.value}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    result = await Transaction.aggregate(pipeline).to_list()
    return result[0]["total"] if result else 0


class ProfilingSummary(BaseModel):
    year: int
    month: int
    current_month_purchase: int
    moving_average: float
    window_months: int
    ratio: Optional[float] = None
    is_alert: bool = False


async def _compute_profiling(year: int, month: int) -> ProfilingSummary:
    current_total = await _get_monthly_purchase_total(year, month)

    prev_totals = []
    for i in range(1, MOVING_AVERAGE_WINDOW_MONTHS + 1):
        py, pm = _shift_month(year, month, -i)
        prev_totals.append(await _get_monthly_purchase_total(py, pm))

    moving_average = sum(prev_totals) / len(prev_totals) if prev_totals else 0.0
    ratio = (current_total / moving_average) if moving_average > 0 else None
    is_alert = ratio is not None and ratio > ALERT_THRESHOLD_RATIO

    return ProfilingSummary(
        year=year,
        month=month,
        current_month_purchase=current_total,
        moving_average=round(moving_average, 2),
        window_months=MOVING_AVERAGE_WINDOW_MONTHS,
        ratio=round(ratio, 4) if ratio is not None else None,
        is_alert=is_alert,
    )

# TASK 1 POIN 2: "membuat fungsi untuk profiling summary"
@app.get("/profiling/summary", response_model=ProfilingSummary)
async def get_profiling_summary(year: int, month: int):
    return await _compute_profiling(year, month)


class AlertCheckResult(BaseModel):
    triggered: bool
    summary: ProfilingSummary


# TASK 1 POIN 3: "membuat fungsi alert on hit"
@app.post("/profiling/check-alert", response_model=AlertCheckResult)
async def check_alert(year: Optional[int] = None, month: Optional[int] = None):
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    summary = await _compute_profiling(year, month)
    return AlertCheckResult(triggered=summary.is_alert, summary=summary)