import json
from test_app import app, client, login
from test_sku_comparison import seed
from models import AccountItem


def test_search_finds_variation_sku_beyond_first_page_and_is_seller_scoped(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        source=db.get(AccountItem,'MLB123456789')
        for n in range(105):
            values={c.name:getattr(source,c.name) for c in AccountItem.__table__.columns}
            values.update(mlb=f'MLB90000{n:04}',skus_json=json.dumps(['Common', 'Zero-001' if n==104 else 'OTHER']))
            db.add(AccountItem(**values))
        values.update(mlb='MLB999999999',seller_id='another',skus_json='["Zero-001"]')
        db.add(AccountItem(**values))
    text=client.get('/catalog?q=zero-001').get_data(as_text=True)
    assert 'MLB900000104' in text and 'MLB999999999' not in text and '1 anúncio(s)' in text
    text=client.get('/catalog?q=900000104').get_data(as_text=True)
    assert 'Zero-001' in text
    text=client.get('/catalog?q=Common').get_data(as_text=True)
    assert 'q=Common' in text and 'Página 1 de 2' in text
    text=client.get('/catalog?q=Common&page=2').get_data(as_text=True)
    assert 'MLB900000104' in text and 'MLB900000000' not in text


def test_search_is_literal_and_does_not_confuse_custom_field_with_sku(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        item=db.get(AccountItem,'MLB123456789')
        item.skus_json='["SKU%_01"]'
        item.custom_fields_json='["NOT-A-SKU"]'
    assert 'MLB123456789' in client.get('/catalog?q=SKU%25_01').get_data(as_text=True)
    assert 'Nenhum anúncio encontrado' in client.get('/catalog?q=NOT-A-SKU').get_data(as_text=True)
    assert 'Nenhum anúncio encontrado' in client.get('/catalog?q=nonexistent').get_data(as_text=True)
