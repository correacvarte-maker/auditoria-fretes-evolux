from test_app import app, client, login


def test_delayed_assets_cannot_overwrite_rotated_login_cookie(app, client):
    client.get('/login')
    old_cookie = client.get_cookie('evolux_session').value
    login(client)
    # A stylesheet or favicon can finish after the login POST rotates the cookie.
    late_request = app.test_client()
    late_request.set_cookie('evolux_session', old_cookie)
    for path in ('/static/style.css', '/favicon.ico', '/static/missing.css'):
        response = late_request.get(path)
        assert 'Set-Cookie' not in response.headers


def test_login_url_preserves_existing_authenticated_session(client):
    login(client)
    response = client.get('/login')
    assert response.status_code == 302
    assert response.headers['Location'] == '/'
    assert client.get('/').status_code == 200
