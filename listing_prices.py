"""Winning, unit marketplace price; never infer promotions from missing data.

Source: /items/{id}/sale_price?context=channel_marketplace&quantity=1
https://developers.mercadolivre.com.br/en_us/price-apl
"""
from decimal import Decimal, InvalidOperation


def amount(raw):
    try:
        value = Decimal(str(raw))
        if value.is_finite() and 0 < value <= Decimal('9999999999.99') and value == value.quantize(Decimal('.01')):
            return value.quantize(Decimal('.01'))
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def read_prices(body):
    unavailable = {'full': None, 'promotional': None, 'effective': None, 'state': 'unavailable',
                   'note': 'Preço de venda indisponível na API'}
    if not isinstance(body, dict) or body.get('currency_id') != 'BRL' or amount(body.get('amount')) is None:
        return unavailable
    metadata = body.get('metadata')
    if isinstance(metadata, dict) and metadata.get('is_price_per_quantity') is True:
        return {**unavailable, 'note': 'Preço por quantidade não usado como preço unitário'}
    effective = amount(body['amount'])
    full = amount(body.get('regular_amount'))
    if full is not None and full > effective:
        return {'full': full, 'promotional': effective, 'effective': effective, 'state': 'promotion',
                'note': 'Preço promocional vigente no Mercado Livre · 1 unidade'}
    if ('regular_amount' in body and body['regular_amount'] is None) or full == effective:
        return {'full': effective, 'promotional': None, 'effective': effective, 'state': 'standard',
                'note': 'Sem promoção no preço consultado · 1 unidade'}
    return {'full': None, 'promotional': None, 'effective': effective, 'state': 'partial',
            'note': 'Preço vigente disponível; preço cheio e promoção não confirmados'}


def price_columns(item, snapshot):
    if not snapshot or snapshot.seen_run != item.seen_run or snapshot.seller_id != item.seller_id:
        return {'full_price': None, 'promotional_price': None, 'effective_price': None,
                'price_state': 'unavailable', 'price_note': 'Atualize os anúncios para consultar os preços', 'price_consulted_at': None}
    return {'full_price': snapshot.full, 'promotional_price': snapshot.promotional,
            'effective_price': snapshot.effective, 'price_state': snapshot.state,
            'price_note': snapshot.note, 'price_consulted_at': snapshot.consulted_at}
