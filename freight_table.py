"""Standard freight table by unit selling price, verified 2026-10-20.

Source: https://www.mercadolivre.com.br/ajuda/40538
New products / Full, Coleta and agencies / green or no reputation, MercadoLider.
Prices already include the reputation discount. Never discount them a second time.
The current marketplace price selects one of eight columns. The >=200 column
is retained in BANDS for weight bounds; tariffs come from freight_rates.json.
Optional fast free shipping below R$79 is a separate table, not inferred here.
"""
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path

SOURCE_URL = 'https://www.mercadolivre.com.br/ajuda/40538'
CHECKED_ON = '20/10/2026'
RAW_BANDS = (
    ('0.3', '21.65'), ('0.5', '23.25'), ('1', '24.45'), ('1.5', '25.45'),
    ('2', '25.55'), ('3', '27.05'), ('4', '29.25'), ('5', '32.45'),
    ('6', '40.85'), ('7', '45.25'), ('8', '49.35'), ('9', '50.75'),
    ('10', '65.85'), ('11', '69.35'), ('13', '70.95'), ('15', '75.65'),
    ('17', '79.35'), ('20', '91.95'), ('25', '106.85'), ('30', '107.85'),
    ('40', '107.95'), ('50', '111.65'), ('60', '119.05'), ('70', '127.45'),
    ('80', '133.15'), ('90', '147.85'), ('100', '168.85'), ('125', '188.85'),
    ('150', '200.35'), (None, '262.85'),
)
BANDS = tuple((Decimal(limit) if limit else None, Decimal(cost)) for limit, cost in RAW_BANDS)


PRICE_DATA = json.loads(Path(__file__).with_name('freight_rates.json').read_text(encoding='utf-8'))
PRICE_LIMITS = tuple(Decimal(v) for v in ('19', '49', '79', '100', '120', '150', '200'))


def calculate(product, price=None):
    """Do not round before selecting the inclusive upper-bound weight band."""
    cubic = Decimal(product.width) * Decimal(product.height) * Decimal(product.length) / Decimal(6000)
    gross = Decimal(product.weight)
    considered = max(cubic, gross)
    lower = Decimal(0)
    for band_index, (upper, cost) in enumerate(BANDS):
        if upper is None or considered <= upper:
            label = ('Mais de 150 kg' if upper is None else
                     f'Até {upper} kg' if lower == 0 else f'Acima de {lower} até {upper} kg')
            price_band = None
            expected = None
            if price is not None and Decimal(price).is_finite() and Decimal(price) > 0:
                price = Decimal(price)
                column = next((n for n, limit in enumerate(PRICE_LIMITS) if price < limit), 7)
                price_band = PRICE_DATA['headers'][column].replace('*', '')
                expected = Decimal(PRICE_DATA['rates'][band_index][column])
                if price < 19:
                    expected = min(expected, (price / 2).quantize(Decimal('.01'), rounding=ROUND_DOWN))
            return {'cubic_weight': cubic, 'gross_weight': gross, 'considered_weight': considered,
                    'expected': expected, 'band': label.replace('.', ','), 'price_band': price_band}
        lower = upper
    raise ValueError('Invalid weight')
