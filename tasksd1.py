import io
import os
from datetime import datetime, date as date_type
from enum import Enum
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from beanie import Document, PydanticObjectId, init_beanie
from pymongo import AsyncMongoClient

load_dotenv()

app = FastAPI()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "bootcamp")

# TASK 1: "Ferdi wants the input to be perfectly logical"
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
        name = "trx_collection"


class RequestNewTransaction(BaseModel):
    amount: int = Field(ge=1, description="Amount transaksi. Minimum Rp1, tidak ada batas maksimum.")
    method: PaymentMethod
    desc: str
    trx_type: TrxType
    date: Optional[date_type] = Field(
        default=None,
        description="Tanggal transaksi, format YYYY-MM-DD (tanpa jam). Kalau tidak diisi, otomatis pakai tanggal saat request dikirim.",
    )

#API update
class RequestUpdateTransaction(BaseModel):
    amount: Optional[int] = Field(default=None, ge=1, description="Amount transaksi. Minimum Rp1.")
    method: Optional[PaymentMethod] = None
    desc: Optional[str] = None
    trx_type: Optional[TrxType] = None
    date: Optional[date_type] = None

@app.on_event("startup")
async def init_db():
    if not MONGODB_URI:
        raise RuntimeError(
            "Environment variable MONGODB_URI belum diset. "
            "Untuk lokal, isi file .env (lihat .env.example). "
            "Untuk container, gunakan --env-file/-e. "
            "Untuk OpenShift, pastikan Secret sudah dibuat dan di-mount ke Deployment."
        )

    client = AsyncMongoClient(MONGODB_URI)
    await init_beanie(database=client[MONGODB_DB_NAME], document_models=[Transaction])

# response jika input tidak valid
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        field_path = ".".join(str(loc) for loc in err["loc"] if loc != "body")
        errors.append({"field": field_path, "message": err["msg"]})
 
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "message": "Input tidak valid",
            "errors": errors,
        },
    )

@app.post("/transaction/add")
async def add_transaction(request_body: RequestNewTransaction):
    trx_date = (
        datetime.combine(request_body.date, datetime.min.time())
        if request_body.date
        else datetime.now()
    )
    trx = Transaction(
        date=trx_date,
        amount=request_body.amount,
        method=request_body.method,
        desc=request_body.desc,
        trx_type=request_body.trx_type,
    )
    await trx.insert()
    return trx

@app.get("/transaction")
async def get_transaction(start_date: datetime, end_date: datetime):
    return await Transaction.find(
        Transaction.date >= start_date, Transaction.date <= end_date
    ).to_list()


@app.get("/transaction/summary")
async def summary_by_method(year: int, month: int):
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)

    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": "$trx_type",
                "total_amount": {"$sum": "$amount"},
                "count": {"$sum": 1},
            }
        },
    ]

    return await Transaction.aggregate(pipeline).to_list()

# TASK 2: "Ferdi doesn't want only a summary by month, but also want to know if he is a reckless spender or a big saver guy"
@app.get("/transaction/insight")
async def get_insight(year: int, month: int):
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)

    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": "$trx_type",
                "total_amount": {"$sum": "$amount"},
                "count": {"$sum": 1},
            }
        },
    ]

    raw_summary = await Transaction.aggregate(pipeline).to_list()

    income_total = 0
    purchase_total = 0
    for row in raw_summary:
        if row["_id"] == TrxType.income.value:
            income_total = row["total_amount"]
        elif row["_id"] == TrxType.purchase.value:
            purchase_total = row["total_amount"]

    net_amount = income_total - purchase_total

    if income_total == 0:
        spend_ratio: Optional[float] = None
        if purchase_total == 0:
            label = "No transactions recorded this month"
        else:
            label = "Reckless Spender"
    else:
        spend_ratio = purchase_total / income_total
        if spend_ratio >= 0.70:
            label = "Reckless Spender"
        elif spend_ratio >= 0.40:
            label = "Indikasi Big Spender"
        else:
            label = "Big Saver"

    return {
        "year": year,
        "month": month,
        "income_total": income_total,
        "purchase_total": purchase_total,
        "net_amount": net_amount,
        "spend_ratio": spend_ratio,
        "label": label,
        "summary_per_type": raw_summary,
    }

# Membersihkan yang ada duplicate
@app.delete("/transaction/duplicates")
async def delete_duplicate_transactions():
    pipeline = [
        {
            "$group": {
                "_id": {
                    "date": "$date",
                    "amount": "$amount",
                    "method": "$method",
                    "desc": "$desc",
                    "trx_type": "$trx_type",
                },
                "ids": {"$push": "$_id"},
                "count": {"$sum": 1},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
 
    duplicate_groups = await Transaction.aggregate(pipeline).to_list()
 
    deleted_ids = []
    for group in duplicate_groups:
        ids_in_group = group["ids"]
        ids_to_delete = ids_in_group[1:]  # simpan 1, hapus sisanya
        for doc_id in ids_to_delete:
            trx = await Transaction.get(doc_id)
            if trx:
                await trx.delete()
                deleted_ids.append(str(doc_id))
 
    return {
        "duplicate_groups_found": len(duplicate_groups),
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
    }

# API update jika ada kesalahan input
@app.get("/transaction/{trx_id}")
async def get_transaction_by_id(trx_id: PydanticObjectId):
    trx = await Transaction.get(trx_id)
    if not trx:
        raise HTTPException(status_code=404, detail=f"Transaksi dengan id {trx_id} tidak ditemukan")
    return trx
 
 
@app.put("/transaction/{trx_id}")
async def update_transaction(trx_id: PydanticObjectId, request_body: RequestUpdateTransaction):
    trx = await Transaction.get(trx_id)
    if not trx:
        raise HTTPException(status_code=404, detail=f"Transaksi dengan id {trx_id} tidak ditemukan")
 
    update_data = request_body.model_dump(exclude_unset=True, exclude_none=True)
 
    if "date" in update_data and update_data["date"] is not None:
        update_data["date"] = datetime.combine(update_data["date"], datetime.min.time())
 
    for field, value in update_data.items():
        setattr(trx, field, value)
 
    await trx.save()
    return trx

# API delete jika ada kesalahan input
@app.delete("/transaction/{trx_id}")
async def delete_transaction(trx_id: PydanticObjectId):
    trx = await Transaction.get(trx_id)
    if not trx:
        raise HTTPException(status_code=404, detail=f"Transaksi dengan id {trx_id} tidak ditemukan")
 
    await trx.delete()
    return {"success": True, "message": "Transaksi berhasil dihapus", "id": str(trx_id)}
    
# TASK 3: "Ferdi has an idea, he already saved his old transaction using excel file, and want to migrate the excel data using your app!"
REQUIRED_COLUMNS = {"date", "amount", "method", "desc"}

@app.post("/transaction/import-excel")
async def import_excel(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="File harus berformat .xlsx atau .xls")

    content = await file.read()
    df = pd.read_excel(io.BytesIO(content))
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing_columns = REQUIRED_COLUMNS - set(df.columns)
    if missing_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Kolom berikut tidak ditemukan di excel: {sorted(missing_columns)}",
        )

    valid_transactions = []
    failed_rows = []

    for idx, row in df.iterrows():
        excel_row_number = idx + 2  # +2 karena idx mulai dari 0 dan baris 1 adalah header
        try:
            trx_date = pd.to_datetime(row["date"]).to_pydatetime()
            raw_amount = int(row["amount"])
            method = PaymentMethod(str(row["method"]).strip().lower())
            desc = str(row["desc"]).strip()

            if raw_amount == 0:
                raise ValueError("amount tidak boleh 0 (tidak jelas ini income atau purchase)")

            trx_type = TrxType.purchase if raw_amount < 0 else TrxType.income
            amount = abs(raw_amount)
            
            valid_transactions.append(
                Transaction(
                    date=trx_date,
                    amount=amount,
                    method=method,
                    desc=desc,
                    trx_type=trx_type,
                )
            )
        except Exception as e:
            failed_rows.append({"row": excel_row_number, "reason": str(e)})

    if valid_transactions:
        await Transaction.insert_many(valid_transactions)

    return {
        "total_rows_in_file": len(df),
        "success_count": len(valid_transactions),
        "failed_count": len(failed_rows),
        "failed_rows": failed_rows,
    }