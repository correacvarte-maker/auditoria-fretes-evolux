from sqlalchemy import Column, Integer, String, Text, Numeric, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class MeasureUpload(Base):
    __tablename__ = 'measure_uploads'
    session_id = Column(String(64), primary_key=True)
    seller_id = Column(String(32), nullable=False)
    rows_json = Column(Text, nullable=False)
    expires = Column(Integer, nullable=False)


class BrowserSession(Base):
    __tablename__ = 'browser_sessions'
    id = Column(String(64), primary_key=True)  # hash of opaque cookie
    csrf = Column(String(64), nullable=False)
    authenticated = Column(Integer, nullable=False, default=0)
    expires = Column(Integer, nullable=False)


class LoginGuard(Base):
    __tablename__ = 'login_guard'
    id = Column(Integer, primary_key=True)
    failures = Column(Integer, nullable=False, default=0)
    locked_until = Column(Integer, nullable=False, default=0)


class OAuthState(Base):
    __tablename__ = 'oauth_states'
    id = Column(String(64), primary_key=True)
    session_id = Column(String(64), nullable=False)
    verifier = Column(Text, nullable=False)  # encrypted PKCE verifier
    expires = Column(Integer, nullable=False)


class Connection(Base):
    __tablename__ = 'meli_connection'
    id = Column(Integer, primary_key=True)
    seller_id = Column(String(32), nullable=False)
    access = Column(Text, nullable=False)
    refresh = Column(Text, nullable=False)
    expires = Column(Integer, nullable=False)


class PendingConnection(Base):
    __tablename__ = 'pending_meli_connections'
    session_id = Column(String(64), primary_key=True)
    seller_id = Column(String(32), nullable=False)
    nickname = Column(String(120), nullable=False)
    access = Column(Text, nullable=False)
    refresh = Column(Text, nullable=False)
    expires = Column(Integer, nullable=False)
    confirm_until = Column(Integer, nullable=False)


class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    sku = Column(String(80), unique=True, nullable=False)
    name = Column(String(180), nullable=False)
    length = Column(Numeric(10, 2), nullable=False)
    width = Column(Numeric(10, 2), nullable=False)
    height = Column(Numeric(10, 2), nullable=False)
    weight = Column(Numeric(10, 3), nullable=False)


class AdTemplate(Base):
    __tablename__ = 'ad_templates'
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    title = Column(String(180), nullable=False)
    body = Column(Text, nullable=False)


class Listing(Base):
    __tablename__ = 'listings'
    id = Column(Integer, primary_key=True)
    mlb = Column(String(32), unique=True, nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    template_id = Column(Integer, ForeignKey('ad_templates.id'))
    title = Column(String(180), nullable=False)
    listing_type = Column(String(40), nullable=False)
    reported_dimensions = Column(Text, nullable=False, default='Não informadas')
    synced_at = Column(Integer)
    product = relationship(Product)
    template = relationship(AdTemplate)


class Freight(Base):
    __tablename__ = 'freights'
    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey('listings.id'), nullable=False)
    reference = Column(String(100), unique=True, nullable=False)
    date = Column(String(10), nullable=False)
    expected = Column(Numeric(12, 2), nullable=False)
    charged = Column(Numeric(12, 2), nullable=False)
    source = Column(String(180), nullable=False)
    listing = relationship(Listing)

    @property
    def difference(self):
        return self.charged - self.expected


class AccountItem(Base):
    __tablename__ = 'account_items'
    mlb = Column(String(32), primary_key=True)
    seller_id = Column(String(32), nullable=False, index=True)
    title = Column(String(180), nullable=False)
    status = Column(String(40), nullable=False)
    listing_type = Column(String(40), nullable=False)
    skus_json = Column(Text, nullable=False, default='[]')
    custom_fields_json = Column(Text, nullable=False, default='[]')
    dimensions = Column(Text, nullable=False)
    shipping_mode = Column(String(40), nullable=False)
    logistic_type = Column(String(40), nullable=False)
    free_shipping = Column(Integer, nullable=False)
    price = Column(Numeric(12, 2))
    quote = Column(Numeric(12, 2))
    expected_quote = Column(Numeric(12, 2))
    quote_note = Column(String(180), nullable=False)
    reference_note = Column(String(180), nullable=False)
    billable_weight = Column(Numeric(12, 3))
    seen_run = Column(String(64), nullable=False)
    synced_at = Column(Integer, nullable=False)


class CatalogImport(Base):
    __tablename__ = 'catalog_imports'
    id = Column(Integer, primary_key=True)
    seller_id = Column(String(32), nullable=False)
    run_id = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False)
    phase = Column(String(180), nullable=False)
    total = Column(Integer, nullable=False, default=0)
    processed = Column(Integer, nullable=False, default=0)
    quotes = Column(Integer, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    started_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)


class ItemPublication(Base):
    __tablename__ = 'item_publication_types'
    mlb = Column(String(32), primary_key=True)
    seller_id = Column(String(32), nullable=False)
    catalog_listing = Column(Integer)  # 1 catalog, 0 traditional, NULL unknown
    seen_run = Column(String(64), nullable=False)


class ItemPrice(Base):
    __tablename__ = 'item_marketplace_prices'
    mlb = Column(String(32), primary_key=True)
    seller_id = Column(String(32), nullable=False)
    full = Column(Numeric(12, 2))
    promotional = Column(Numeric(12, 2))
    effective = Column(Numeric(12, 2))
    state = Column(String(30), nullable=False)
    note = Column(String(180), nullable=False)
    seen_run = Column(String(64), nullable=False)
    consulted_at = Column(Integer, nullable=False)


class SkuQuote(Base):
    __tablename__ = 'sku_reference_quotes'
    mlb = Column(String(32), primary_key=True)
    sku = Column(String(80), primary_key=True)
    seller_id = Column(String(32), nullable=False)
    expected = Column(Numeric(12, 2))
    billable_weight = Column(Numeric(12, 3))
    measure_signature = Column(String(64), nullable=False)
    note = Column(String(180), nullable=False)
    seen_run = Column(String(64), nullable=False)
    calculated_at = Column(Integer, nullable=False)
