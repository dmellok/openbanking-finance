"""Generate a demo SQLite database for screenshots.

Run with `DATABASE_URL=sqlite:///./demo.db uv run python scripts/seed_demo.py`.
Drops and recreates every table, then writes ~14 months of plausible
transactions, accounts, balance snapshots and a handful of trades.

All names, amounts and IDs are fictitious.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, SQLModel, create_engine, delete

from app.config import get_settings
from app.models import (
    Account,
    BalanceSnapshot,
    Connection,
    SyncRun,
    Trade,
    Transaction,
)

SEED = 42
MONTHS = 14
TODAY = date(2026, 5, 12)
START = TODAY - timedelta(days=30 * MONTHS)

_SALARY = Decimal("4250.00")
_RENT = Decimal("-2200.00")
_OPENING_EVERYDAY = Decimal("3500.00")
_OPENING_SAVER = Decimal("18000.00")
_OPENING_CREDIT = Decimal("0.00")
_OPENING_INVEST = Decimal("12500.00")

_GROCERY_MERCHANTS = ["Coles", "Woolworths", "ALDI", "IGA"]
_CAFES = ["Industry Beans", "Patricia Coffee", "Market Lane", "Dukes Coffee", "Seven Seeds"]
_RESTAURANTS = ["Chin Chin", "Tipo 00", "Cumulus Inc", "Hakata Gensuke", "Lune Croissanterie"]
_TRANSPORT = ["Uber", "Myki Top-Up", "DiDi", "BP Connect", "7-Eleven Fuel"]
_SUBS = [
    ("Spotify", Decimal("-12.99")),
    ("Netflix", Decimal("-22.99")),
    ("Disney+", Decimal("-13.99")),
    ("Kindle Unlimited", Decimal("-13.99")),
    ("iCloud+ 200GB", Decimal("-4.49")),
]
_BILLS = [
    ("AGL Electricity", "BILLS"),
    ("Origin Gas", "BILLS"),
    ("Belong Mobile", "BILLS"),
    ("Aussie Broadband", "BILLS"),
]
_SHOPPING = ["Kmart", "Bunnings Warehouse", "JB Hi-Fi", "Officeworks", "Uniqlo", "Cotton On"]
_HEALTH = ["Chemist Warehouse", "Priceline", "Better Health Clinic"]


def main() -> None:
    rng = random.Random(SEED)

    settings = get_settings()
    engine = create_engine(settings.database_url, echo=False)
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        # Make script idempotent if someone runs it without drop_all (e.g. against
        # an existing demo.db). drop_all above usually covers this, but be safe.
        for model in (Trade, Transaction, BalanceSnapshot, Account, Connection, SyncRun):
            session.exec(delete(model))
        session.commit()

        bank = Connection(
            id=str(uuid4()),
            provider="demo",
            category="banking",
            institution_id="demo-bank",
            institution_name="Demo Bank",
            status="active",
            last_refreshed_at=datetime.now(UTC),
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
        broker = Connection(
            id=str(uuid4()),
            provider="demo",
            category="brokerage",
            institution_id="demo-broker",
            institution_name="Demo Broker",
            status="active",
            last_refreshed_at=datetime.now(UTC),
            created_at=datetime.now(UTC) - timedelta(days=300),
        )
        session.add(bank)
        session.add(broker)

        everyday = _account(bank.id, "Everyday", "transaction", "****1234")
        saver = _account(bank.id, "High Interest Saver", "savings", "****5678")
        credit = _account(bank.id, "Credit Visa", "credit-card", "****9012")
        invest = _account(broker.id, "Brokerage Portfolio", "investment", "****3456")
        for a in (everyday, saver, credit, invest):
            session.add(a)

        balances = {
            everyday.id: _OPENING_EVERYDAY,
            saver.id: _OPENING_SAVER,
            credit.id: _OPENING_CREDIT,
            invest.id: _OPENING_INVEST,
        }

        snapshots: list[BalanceSnapshot] = []
        transactions: list[Transaction] = []

        d = START
        while d <= TODAY:
            _emit_day(d, rng, everyday, saver, credit, balances, transactions)

            # End-of-day balance snapshot for each account
            taken = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23)
            for acc in (everyday, saver, credit, invest):
                snapshots.append(
                    BalanceSnapshot(
                        account_id=acc.id,
                        current_balance=balances[acc.id],
                        available_balance=balances[acc.id],
                        currency="AUD",
                        taken_at=taken,
                    )
                )
            d += timedelta(days=1)

        # Investment account: slow, noisy growth + a few discrete trades.
        _add_trades(invest, transactions, rng, session)
        _drift_investment_balance(invest, snapshots, rng)

        session.add_all(transactions)
        session.add_all(snapshots)

        session.add(
            SyncRun(
                kind="poll",
                started_at=datetime.now(UTC) - timedelta(minutes=15),
                finished_at=datetime.now(UTC) - timedelta(minutes=14),
                status="ok",
                detail=None,
                counts={
                    "connections": 2,
                    "accounts": 4,
                    "balances": 4,
                    "transactions": len(transactions),
                    "trades": 0,
                    "failed_accounts": 0,
                    "skipped_accounts": 0,
                },
            )
        )

        session.commit()
        print(
            f"Seeded demo.db: {len(transactions)} transactions, "
            f"{len(snapshots)} balance snapshots, 4 accounts, 2 connections."
        )


def _account(connection_id: str, name: str, type_: str, masked: str) -> Account:
    return Account(
        id=str(uuid4()),
        connection_id=connection_id,
        provider="demo",
        name=name,
        masked_number=masked,
        type=type_,
        institution_name="Demo Bank" if type_ != "investment" else "Demo Broker",
        currency="AUD",
        last_polled_transactions_at=datetime.now(UTC),
        last_polled_trades_at=datetime.now(UTC),
    )


_HOUR_RNG = random.Random(SEED + 1)


def _tx(
    account_id: str,
    when: date,
    amount: Decimal,
    description: str,
    category: str,
    merchant: str | None = None,
) -> Transaction:
    # Spread posted_at across plausible hours so the time-of-day heatmap is
    # interesting. Groceries weekends, lunch midday, dinner evening, coffee 8am.
    if category == "FOOD_AND_DRINK":
        hour = _HOUR_RNG.choice([8, 8, 9, 12, 12, 13, 13, 18, 19, 20])
    elif category == "GROCERIES":
        hour = _HOUR_RNG.choice([10, 11, 14, 16, 17, 18, 19])
    elif category == "TRANSPORT":
        hour = _HOUR_RNG.choice([7, 8, 8, 9, 17, 18, 18, 19, 22])
    elif category == "SHOPPING":
        hour = _HOUR_RNG.choice([11, 12, 14, 15, 16, 17])
    elif category == "INCOME":
        hour = 9  # salary lands morning of payday
    else:
        hour = _HOUR_RNG.randint(7, 21)
    minute = _HOUR_RNG.randint(0, 59)
    return Transaction(
        id=str(uuid4()),
        account_id=account_id,
        status="posted",
        posted_at=datetime.combine(when, datetime.min.time(), tzinfo=UTC).replace(
            hour=hour, minute=minute
        ),
        local_date=when,
        amount=amount,
        currency="AUD",
        direction="credit" if amount > 0 else "debit",
        description=description,
        merchant_name=merchant or description,
        category=category,
    )


def _emit_day(
    d: date,
    rng: random.Random,
    everyday: Account,
    saver: Account,
    credit: Account,
    balances: dict[str, Decimal],
    out: list[Transaction],
) -> None:
    # Salary every second Thursday
    if d.weekday() == 3 and (d.toordinal() // 7) % 2 == 0:
        out.append(_tx(everyday.id, d, _SALARY, "ACME Pty Ltd Salary", "INCOME"))
        balances[everyday.id] += _SALARY

        # Auto-transfer to saver
        save_amt = Decimal("-800.00")
        out.append(_tx(everyday.id, d, save_amt, "Transfer to Saver", "TRANSFER"))
        out.append(_tx(saver.id, d, -save_amt, "Transfer from Everyday", "TRANSFER"))
        balances[everyday.id] += save_amt
        balances[saver.id] += -save_amt

    # Monthly rent (1st of month)
    if d.day == 1:
        out.append(_tx(everyday.id, d, _RENT, "Rent — apartment", "RENT"))
        balances[everyday.id] += _RENT

    # Monthly utilities/bills around the 10th
    if d.day == 10:
        for name, cat in _BILLS:
            amt = Decimal(-rng.randint(45, 220))
            out.append(_tx(everyday.id, d, amt, name, cat))
            balances[everyday.id] += amt

    # Subscriptions on rotating days
    for i, (name, amt) in enumerate(_SUBS):
        if d.day == 15 + i:
            out.append(_tx(credit.id, d, amt, name, "ENTERTAINMENT"))
            balances[credit.id] += amt

    # Saver interest on the last day of each month
    next_day = d + timedelta(days=1)
    if next_day.day == 1:
        interest = (balances[saver.id] * Decimal("0.00375")).quantize(Decimal("0.01"))
        if interest > 0:
            out.append(_tx(saver.id, d, interest, "Interest credited", "INCOME"))
            balances[saver.id] += interest

    # Credit card auto-payment around the 20th — pay off prior balance
    if d.day == 20 and balances[credit.id] < 0:
        pay = -balances[credit.id]
        out.append(_tx(everyday.id, d, -pay, "Credit Card Payment", "TRANSFER"))
        out.append(_tx(credit.id, d, pay, "Payment received", "TRANSFER"))
        balances[everyday.id] -= pay
        balances[credit.id] += pay

    # Daily discretionary
    # Groceries 2-3x/week, mostly weekends
    if rng.random() < (0.55 if d.weekday() >= 5 else 0.25):
        merchant = rng.choice(_GROCERY_MERCHANTS)
        amt = Decimal(-rng.randint(35, 180)) + Decimal(rng.randint(0, 99)) / Decimal(100)
        out.append(_tx(credit.id, d, amt, merchant, "GROCERIES", merchant))
        balances[credit.id] += amt

    # Coffee on weekday mornings
    if d.weekday() < 5 and rng.random() < 0.7:
        merchant = rng.choice(_CAFES)
        amt = Decimal(f"-{rng.uniform(4.5, 6.5):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "FOOD_AND_DRINK", merchant))
        balances[credit.id] += amt

    # Lunch sometimes
    if d.weekday() < 5 and rng.random() < 0.5:
        merchant = rng.choice(_RESTAURANTS + ["Mr Tulk", "Hardware Société"])
        amt = Decimal(f"-{rng.uniform(15, 35):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "FOOD_AND_DRINK", merchant))
        balances[credit.id] += amt

    # Dinner out occasionally
    if rng.random() < 0.18:
        merchant = rng.choice(_RESTAURANTS)
        amt = Decimal(f"-{rng.uniform(45, 120):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "FOOD_AND_DRINK", merchant))
        balances[credit.id] += amt

    # Transport
    if rng.random() < 0.45:
        merchant = rng.choice(_TRANSPORT)
        amt = Decimal(f"-{rng.uniform(6, 65):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "TRANSPORT", merchant))
        balances[credit.id] += amt

    # Shopping
    if rng.random() < 0.10:
        merchant = rng.choice(_SHOPPING)
        amt = Decimal(f"-{rng.uniform(20, 250):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "SHOPPING", merchant))
        balances[credit.id] += amt

    # Health
    if rng.random() < 0.04:
        merchant = rng.choice(_HEALTH)
        amt = Decimal(f"-{rng.uniform(10, 90):.2f}")
        out.append(_tx(credit.id, d, amt, merchant, "HEALTH", merchant))
        balances[credit.id] += amt


def _add_trades(
    invest: Account,
    transactions: list[Transaction],
    rng: random.Random,
    session: Session,
) -> None:
    symbols = [
        ("VAS.AX", "Vanguard Australian Shares ETF"),
        ("VGS.AX", "Vanguard MSCI Intl Shares ETF"),
        ("A200.AX", "BetaShares Australia 200 ETF"),
        ("NDQ.AX", "BetaShares NASDAQ 100 ETF"),
    ]
    d = START
    while d <= TODAY:
        # Roughly monthly DCA on the 5th
        if d.day == 5:
            sym, name = rng.choice(symbols)
            qty = Decimal(rng.randint(2, 8))
            price = Decimal(f"{rng.uniform(85, 135):.2f}")
            total = (qty * price).quantize(Decimal("0.01"))
            session.add(
                Trade(
                    id=str(uuid4()),
                    account_id=invest.id,
                    symbol=sym,
                    name=name,
                    type="buy",
                    quantity=qty,
                    price=price,
                    currency="AUD",
                    total_amount=-total,
                    fees=Decimal("9.50"),
                    trade_date=d,
                    settlement_date=d + timedelta(days=2),
                    description=f"BUY {qty} {sym} @ {price}",
                )
            )
            transactions.append(
                _tx(invest.id, d, -total, f"BUY {qty} {sym}", "INVESTMENT", sym)
            )
        d += timedelta(days=1)


def _drift_investment_balance(
    invest: Account,
    snapshots: list[BalanceSnapshot],
    rng: random.Random,
) -> None:
    # Walk the investment balance with a slight upward drift + noise to mimic
    # market movement. Operates in-place on already-built snapshots for this account.
    invest_snaps = [s for s in snapshots if s.account_id == invest.id]
    invest_snaps.sort(key=lambda s: s.taken_at)
    balance = _OPENING_INVEST
    daily_drift = Decimal("0.0003")
    for snap in invest_snaps:
        noise = Decimal(f"{rng.uniform(-0.015, 0.015):.4f}")
        balance = (balance * (Decimal("1") + daily_drift + noise)).quantize(Decimal("0.01"))
        # Also nudge for monthly buys
        if snap.taken_at.day == 5:
            balance += Decimal("400")
        snap.current_balance = balance
        snap.available_balance = balance


if __name__ == "__main__":
    main()
