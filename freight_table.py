"""User-selected reference: >= R$200 column, captured 2026-10-14.

Source: https://www.mercadolivre.com.br/ajuda/40538
New products / Full, Coleta and agencies / green or no reputation, MercadoLider.
Prices already include the reputation discount. Never discount them a second time.
The user explicitly selected this column regardless of the advertisement price.
"""
from decimal import Decimal

SOURCE_URL = 'https://www.mercadolivre.com.br/ajuda/40538'
CHECKED_ON = '14/10/2026'
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


def calculate(product):
    """Do not round before selecting the inclusive upper-bound weight band."""
    cubic = Decimal(product.width) * Decimal(product.height) * Decimal(product.length) / Decimal(6000)
    gross = Decimal(product.weight)
    considered = max(cubic, gross)
    lower = Decimal(0)
    for upper, cost in BANDS:
        if upper is None or considered <= upper:
            label = ('Mais de 150 kg' if upper is None else
                     f'Até {upper} kg' if lower == 0 else f'Acima de {lower} até {upper} kg')
            return {'cubic_weight': cubic, 'gross_weight': gross, 'considered_weight': considered,
                    'expected': cost, 'band': label.replace('.', ',')}
        lower = upper
    raise ValueError('Invalid weight')
