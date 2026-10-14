"""Display imported package measurements without guessing missing values."""
import json
import hashlib
import re
from decimal import Decimal, InvalidOperation


def measurement(raw, weight=False):
    match = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m|kg|g)\s*', raw)
    if not match:
        return None
    amount = Decimal(match.group(1).replace(',', '.'))
    factors = {'g': Decimal('.001'), 'kg': Decimal(1)} if weight else {
        'cm': Decimal(1), 'm': Decimal(100), 'mm': Decimal('.1')}
    if match.group(2) not in factors or not amount.is_finite() or amount <= 0:
        return None
    return amount * factors[match.group(2)]


def display_measure(value, weight=False):
    if value is None:
        return 'Não informado'
    quantum = Decimal('.001') if weight else Decimal('.01')
    try:
        number = format(value.quantize(quantum), 'f').rstrip('0').rstrip('.')
        return number.replace('.', ',') + (' kg' if weight else ' cm')
    except InvalidOperation:
        return 'Não informado'


def package_rows(records):
    rows = []
    for item in records:
        measures = {key: None for key in ('width', 'height', 'length', 'weight')}
        for line in item.dimensions.splitlines():
            for key in measures:
                prefix = 'SELLER_PACKAGE_' + key.upper() + ': '
                if line.startswith(prefix):
                    measures[key] = measurement(line[len(prefix):], key == 'weight')
            prefix = 'shipping.dimensions (cm e g): '
            if line.startswith(prefix):
                match = re.fullmatch(r'(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?),(\d+)', line[len(prefix):])
                if match:
                    for key, raw in zip(('height', 'width', 'length', 'weight'), match.groups()):
                        if measures[key] is None:
                            measures[key] = measurement(raw + (' g' if key == 'weight' else ' cm'), key == 'weight')
        skus = json.loads(item.skus_json)
        for sku in skus or ['SKU não informado']:
            rows.append({'sku': sku, 'mlb': item.mlb, 'title': item.title, 'synced_at': item.synced_at,
                         'width': display_measure(measures['width']),
                         'height': display_measure(measures['height']),
                         'depth': display_measure(measures['length']),
                         'weight': display_measure(measures['weight'], True)})
    return rows


def reference_signature(product):
    values = [product.sku] + [format(Decimal(str(getattr(product, key))).normalize(), 'f')
                             for key in ('width', 'height', 'length', 'weight')]
    return hashlib.sha256('|'.join(values).encode()).hexdigest()


def sku_catalog(records):
    result = {}
    for item in records:
        for sku in json.loads(item.skus_json):
            entry = result.setdefault(sku, {'sku': sku, 'name': item.title, 'mlbs': []})
            entry['mlbs'].append(item.mlb)
    return result
