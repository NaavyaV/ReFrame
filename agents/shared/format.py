"""Formatting shared by the agents' replies."""

from __future__ import annotations

SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$"}


def money(amount: float, currency: str) -> str:
    """$89.99, $100, or 12.50 XYZ for a currency without a symbol."""
    text = f"{amount:,.2f}".removesuffix(".00")
    symbol = SYMBOLS.get(currency)
    return f"{symbol}{text}" if symbol else f"{text} {currency}".strip()
