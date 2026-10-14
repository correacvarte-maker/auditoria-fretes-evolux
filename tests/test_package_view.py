import json
from types import SimpleNamespace

from test_app import app, client, login
from test_catalog import connect
from models import AccountItem
from package_view import package_rows


def record(dimensions, skus=('EV-1', 'EV-2')):
    return SimpleNamespace(mlb='MLB123456789', title='Produto', dimensions=dimensions,
                           skus_json=json.dumps(skus), synced_at=1)


def test_dimensions_axes_units_and_each_variation_sku():
    rows = package_rows([record('SELLER_PACKAGE_WIDTH: 20 cm\nSELLER_PACKAGE_HEIGHT: 1,4 m\n'
                               'SELLER_PACKAGE_LENGTH: 30 mm\nSELLER_PACKAGE_WEIGHT: 2050 g')])
    assert [row['sku'] for row in rows] == ['EV-1', 'EV-2']
    for row in rows:
        assert (row['width'], row['height'], row['depth'], row['weight']) == ('20 cm', '140 cm', '3 cm', '2,05 kg')
        assert row['mlb'] == 'MLB123456789'


def test_partial_dimensions_and_shipping_fallback_not_custom_sku():
    rows = package_rows([record('SELLER_PACKAGE_WIDTH: 22 cm', skus=[])])
    assert rows[0]['sku'] == 'SKU não informado'
    assert rows[0]['width'] == '22 cm' and rows[0]['height'] == 'Não informado'
    rows = package_rows([record('shipping.dimensions (cm e g): 10x20x30,1200')])
    assert (rows[0]['height'], rows[0]['width'], rows[0]['depth'], rows[0]['weight']) == ('10 cm', '20 cm', '30 cm', '1,2 kg')


def test_measures_page_search_and_dashboard_section(app, client):
    assert client.get('/catalog/measures').status_code == 302
    login(client)
    connect(app)
    with app.extensions['db_factory'].begin() as db:
        db.add(AccountItem(mlb='MLB123456789', seller_id='456', title='<script>bad</script>', status='active',
            listing_type='gold_special', skus_json='["EV-001"]', custom_fields_json='["UNRELATED"]',
            dimensions='SELLER_PACKAGE_WIDTH: 20 cm\nSELLER_PACKAGE_WEIGHT: 1200 g',
            shipping_mode='me2', logistic_type='cross_docking', free_shipping=1,
            quote_note='none', reference_note='none', seen_run='x', synced_at=1))
    page = client.get('/catalog/api-measures?q=EV-001').get_data(as_text=True)
    assert all(label in page for label in ('SKU no Mercado Livre', 'Largura', 'Altura', 'Profundidade', 'Peso bruto'))
    assert '20 cm' in page and '1,2 kg' in page and 'Não informado' in page
    assert '<script>bad</script>' not in page and 'UNRELATED' not in page
    assert 'MLB123456789' not in client.get('/catalog/api-measures?q=UNRELATED').get_data(as_text=True)
    assert 'Minhas medidas por SKU' in client.get('/').get_data(as_text=True)
