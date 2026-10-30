"""Stream the site's Rozetka XML through prices and stock from the control sheet."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from xml.etree import ElementTree

OFFER_OPEN = re.compile(rb'^\s*<offer\b[^>]*\bid="([^"]+)"')
PRICE = re.compile(rb'(<price>)[^<]*(</price>)')
PRICE_OLD = re.compile(rb'<price_old(?:\s[^>]*)?>[^<]*</price_old>|<price_old\s*/>')
STOCK = re.compile(rb'(<stock_quantity>)[^<]*(</stock_quantity>)')
TWO = Decimal("0.01")


@dataclass(frozen=True)
class Control:
    rrp: Decimal
    currency: str
    discount: Decimal
    stock: str
    rate: Decimal | None = None
    markup: Decimal = Decimal(0)
    old_price: Decimal | None = None


def decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return Decimal(str(value).replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return default


def cell(row: list, index: int) -> str:
    return str(row[index]).strip() if index < len(row) and row[index] is not None else ""


def load_sheet(path: Path) -> tuple[dict[str, Control], dict[str, Decimal]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    settings = document["settings"]
    rates: dict[str, Decimal] = {}
    for row in settings[3:]:
        key = cell(row, 0)
        rate = decimal(cell(row, 2))
        if key and rate is not None and rate > 0:
            rates[key] = rate
    rates["UAH"] = Decimal(1)

    controls: dict[str, Control] = {}
    for row in document["products"][2:]:
        sku = str(row[6]) if len(row) > 6 and row[6] is not None else ""
        rrp = decimal(cell(row, 11))
        if not sku or rrp is None or rrp < 0:
            continue
        # The legacy table contains one duplicated SKU; its last row controls
        # the existing feed, so keep the same precedence.
        discount = decimal(cell(row, 18), Decimal(0)) or Decimal(0)
        if discount < 0 or discount > 100:
            raise ValueError(f"Invalid discount for SKU {sku}: {discount}")
        stock = cell(row, 23)
        if stock not in ("0", "1", "999"):
            stock = {"Нет в наличии": "0", "Заканчивается": "1", "В наличии": "999"}.get(cell(row, 15), "")
        markup = decimal(cell(row, 22), Decimal(0)) or Decimal(0)
        if markup < 0:
            raise ValueError(f"Invalid markup for SKU {sku}: {markup}")
        old_price = decimal(cell(row, 12))
        if old_price is not None and old_price < 0:
            raise ValueError(f"Invalid old price for SKU {sku}: {old_price}")
        controls[sku] = Control(rrp, cell(row, 13) or "UAH", discount, stock,
                                markup=markup, old_price=old_price or None)
    return controls, rates


def load_controls_csv(path: Path) -> tuple[dict[str, Control], dict[str, Decimal]]:
    controls: dict[str, Control] = {}
    required = {"article", "rrp", "currency", "discount", "stock_quantity", "rate", "markup", "old_price"}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Control CSV columns missing: {sorted(required - set(reader.fieldnames or []))}")
        for line_number, row in enumerate(reader, start=2):
            sku = row["article"]
            if not sku:
                continue
            rrp = decimal(row["rrp"])
            discount = decimal(row["discount"], Decimal(0))
            currency = row["currency"] or "UAH"
            rate = Decimal(1) if currency == "UAH" else decimal(row["rate"])
            markup = decimal(row["markup"], Decimal(0))
            old_price = decimal(row["old_price"])
            stock = row["stock_quantity"]
            if rrp is None or rrp < 0 or discount is None or not (0 <= discount <= 100):
                raise ValueError(f"Invalid price/discount at CSV line {line_number}, SKU {sku}")
            if rate is None or rate <= 0:
                raise ValueError(f"Missing currency rate at CSV line {line_number}, SKU {sku}")
            if markup is None or markup < 0:
                raise ValueError(f"Invalid markup at CSV line {line_number}, SKU {sku}")
            if old_price is not None and old_price < 0:
                raise ValueError(f"Invalid old price at CSV line {line_number}, SKU {sku}")
            if stock not in ("", "0", "1", "999"):
                raise ValueError(f"Invalid stock at CSV line {line_number}, SKU {sku}")
            controls[sku] = Control(rrp, currency, discount, stock, rate, markup, old_price or None)
    if len(controls) < 1000:
        raise ValueError(f"Control CSV contains only {len(controls)} SKUs; refusing incomplete feed")
    return controls, {}


def calculated_prices(control: Control, rates: dict[str, Decimal]) -> tuple[bytes, bytes | None]:
    rate = Decimal(1) if control.currency == "UAH" else (control.rate or rates.get(control.currency))
    if rate is None:
        raise ValueError(f"Missing currency rate for {control.currency}")
    price = (control.rrp * (Decimal(1) - control.discount / Decimal(100))
             * (Decimal(1) + control.markup / Decimal(100)) * rate)
    current = price.quantize(TWO, rounding=ROUND_HALF_EVEN)
    if control.old_price is None:
        return str(current).encode("ascii"), None
    old_base = max(control.old_price, control.rrp)
    old = (old_base * (Decimal(1) + control.markup / Decimal(100)) * rate)
    old = max(old.quantize(TWO, rounding=ROUND_HALF_EVEN), current)
    return str(current).encode("ascii"), str(old).encode("ascii")


def new_price(control: Control, rates: dict[str, Decimal]) -> bytes:
    return calculated_prices(control, rates)[0]


def transform_offer(data: bytes, controls: dict[str, Control], rates: dict[str, Decimal]) -> tuple[bytes, bool]:
    match = OFFER_OPEN.match(data)
    if not match:
        raise ValueError("Malformed offer opening tag")
    sku = match.group(1).decode("utf-8")
    control = controls.get(sku)
    if control is None:
        return data, False
    if not PRICE.search(data):
        raise ValueError(f"Offer {sku} lacks <price>")
    price, old_price = calculated_prices(control, rates)
    data = PRICE.sub(lambda m: m.group(1) + price + m.group(2), data, count=1)
    if old_price is None:
        data = PRICE_OLD.sub(b"", data, count=1)
    else:
        tag = b"<price_old>" + old_price + b"</price_old>"
        if PRICE_OLD.search(data):
            data = PRICE_OLD.sub(lambda _m: tag, data, count=1)
        else:
            data = PRICE.sub(lambda m: m.group(0) + tag, data, count=1)
    if control.stock:
        if not STOCK.search(data):
            raise ValueError(f"Offer {sku} lacks <stock_quantity>")
        data = STOCK.sub(lambda m: m.group(1) + control.stock.encode("ascii") + m.group(2), data, count=1)
    return data, True


def render(source: Path, output: Path, controls: dict[str, Control], rates: dict[str, Decimal]) -> dict:
    total = matched = 0
    buffer = bytearray()
    inside = False
    tmp = output.with_suffix(output.suffix + ".partial")
    with source.open("rb") as reader, tmp.open("wb") as writer:
        for line in reader:
            if not inside:
                if OFFER_OPEN.match(line):
                    inside = True
                    buffer.extend(line)
                else:
                    writer.write(line)
                continue
            buffer.extend(line)
            if b"</offer>" in line:
                rendered, found = transform_offer(bytes(buffer), controls, rates)
                writer.write(rendered)
                total += 1
                matched += found
                buffer.clear()
                inside = False
        if inside or buffer:
            raise ValueError("Unclosed offer in source feed")
    if total < 1000:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Only {total} offers; refusing incomplete feed")
    # Parse the exact uploaded bytes before atomic replacement.
    for _event, node in ElementTree.iterparse(tmp, events=("end",)):
        if node.tag == "offer":
            node.clear()
    tmp.replace(output)
    return {"offers": total, "controlled_offers": matched, "bytes": output.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--sheet-values", type=Path)
    input_group.add_argument("--controls-csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    controls, rates = load_sheet(args.sheet_values) if args.sheet_values else load_controls_csv(args.controls_csv)
    result = render(args.source, args.output, controls, rates)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
