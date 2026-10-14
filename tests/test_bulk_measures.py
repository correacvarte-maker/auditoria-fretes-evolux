import io
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from werkzeug.datastructures import FileStorage
from test_app import app, client, login, post, csrf
from test_sku_comparison import seed
from models import Product, MeasureUpload, Connection
from freight_table import calculate, BANDS
from measure_import import parse, ImportValidationError


def csv_file(rows):
    return io.BytesIO(('sku;largura;altura;comprimento;peso bruto\n' + rows).encode('utf-8-sig'))


def upload(client, rows):
    return client.post('/catalog/measures/import', data={'csrf': csrf(client, '/'),
        'file': (csv_file(rows), 'medidas.csv')})


def test_all_weight_boundaries_and_unrounded_cubic():
    for n, (upper, cost) in enumerate(BANDS):
        if upper is None:
            continue
        product = SimpleNamespace(width=Decimal(1), height=Decimal(1), length=Decimal(1), weight=upper)
        assert calculate(product)['expected'] == cost
        product.weight = upper + Decimal('.0000001')
        assert calculate(product)['expected'] == BANDS[n + 1][1]
    product = SimpleNamespace(width=Decimal(120), height=Decimal(10), length=Decimal(10), weight=Decimal('1.9'))
    assert calculate(product)['considered_weight'] == 2
    assert calculate(product)['expected'] == Decimal('25.55')
    product.weight = Decimal(3)
    assert calculate(product)['considered_weight'] == 3
    assert calculate(product)['expected'] == Decimal('27.05')


def test_bulk_preview_is_atomic_and_recalculates_on_confirmation(app, client):
    login(client)
    seed(app)
    response = upload(client, 'A;120;10;10;1,900\nB;40;10;30;2,500\n')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert '2 SKUs válidos' in text and 'R$ 25,55' in text and 'R$ 27,05' in text
    with app.extensions['db_factory']() as db:
        assert db.scalar(select(Product)) is None
    assert post(client, '/catalog/measures/confirm', {}).status_code == 302
    with app.extensions['db_factory']() as db:
        assert db.scalar(select(Product).where(Product.sku == 'A')).weight == Decimal('1.900')
        assert db.scalar(select(MeasureUpload)) is None
    comparison = client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'R$ 25,55' in comparison and 'R$ 4,45' in comparison and '2.000000' in comparison
    assert post(client, '/catalog/measures/confirm', {}).status_code == 409
    assert upload(client, 'A;120;10;10;1,900\nB;0;10;30;2\n').status_code == 400
    with app.extensions['db_factory']() as db:
        assert db.scalar(select(Product).where(Product.sku == 'B')).weight == Decimal('2.500')
        assert db.scalar(select(MeasureUpload)) is None


@pytest.mark.parametrize('rows', [
    'A;1;2;3;1\nA;2;3;4;2\n', 'UNKNOWN;1;2;3;1\n', 'A;=2+2;2;3;1\n',
    'A;NaN;2;3;1\n', 'A;1;2;3;-1\n', 'A;1;2;3;0.0001\n', 'A;1;2;3;10001\n',
])
def test_invalid_files_never_write(app, client, rows):
    login(client)
    seed(app)
    assert upload(client, rows).status_code == 400
    with app.extensions['db_factory']() as db:
        assert db.scalar(select(Product)) is None


def test_preview_session_csrf_expiry_and_account_pin(app, client):
    login(client)
    seed(app)
    assert client.post('/catalog/measures/import', data={'file': (csv_file('A;1;2;3;1'), 'x.csv')}).status_code == 400
    upload(client, 'A;1;2;3;1\n')
    other = app.test_client()
    login(other)
    assert post(other, '/catalog/measures/confirm', {}).status_code == 409
    with app.extensions['db_factory'].begin() as db:
        db.scalar(select(MeasureUpload)).expires = int(time.time()) - 1
    assert post(client, '/catalog/measures/confirm', {}).status_code == 409
    upload(client, 'A;1;2;3;1\n')
    with app.extensions['db_factory'].begin() as db:
        db.get(Connection, 1).seller_id = 'other'
    assert post(client, '/catalog/measures/confirm', {}).status_code == 409


def test_xlsx_values_formula_numeric_sku_and_zip_limits():
    # Test-only fixture, not a user-facing workbook artifact.
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.append(['sku', 'largura', 'altura', 'comprimento', 'peso bruto'])
    sheet.append(['001', 120, 10, 10, 1.9])
    def file():
        stream = io.BytesIO()
        book.save(stream)
        stream.seek(0)
        return FileStorage(stream=stream, filename='data.xlsx')
    assert parse(file(), {'001'})[0]['sku'] == '001'
    sheet['B2'] = '=120'
    with pytest.raises(ImportValidationError):
        parse(file(), {'001'})
    sheet['B2'], sheet['A2'] = 120, 1
    with pytest.raises(ImportValidationError):
        parse(file(), {'001'})
    with pytest.raises(ImportValidationError):
        parse(FileStorage(stream=io.BytesIO(b'x' * (2 * 1024 * 1024 + 1)), filename='x.csv'), {'001'})


def test_actual_zero_vs_missing_and_reference_independent_of_price(app, client):
    from models import AccountItem
    login(client)
    seed(app)
    upload(client, 'A;120;10;10;1\n')
    post(client, '/catalog/measures/confirm', {})
    with app.extensions['db_factory'].begin() as db:
        item = db.get(AccountItem, 'MLB123456789')
        item.quote, item.price = Decimal(0), Decimal(50)
    text = client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'R$ 25,55' in text and 'R$ -25,55' in text
    with app.extensions['db_factory'].begin() as db:
        db.get(AccountItem, 'MLB123456789').quote = None
    text = client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'Indisponível' in text and 'R$ -25,55' not in text


def test_large_upload_and_missing_file_invalidates_previous_preview(app, client):
    import json
    from models import AccountItem
    login(client)
    seed(app)
    codes = [f'SKU-{n:05}' for n in range(4000)]
    with app.extensions['db_factory'].begin() as db:
        db.get(AccountItem, 'MLB123456789').skus_json = json.dumps(codes)
    response = upload(client, ''.join(f'{sku};120;10;10;1.900\n' for sku in codes))
    assert response.status_code == 200 and '4000 SKUs válidos' in response.get_data(as_text=True)
    assert post(client, '/catalog/measures/import', {}).status_code == 400
    assert post(client, '/catalog/measures/confirm', {}).status_code == 409
