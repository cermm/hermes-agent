"""A bound delivery adopts callbacks registered before its generation was known."""
from tests.gateway.test_post_delivery_callback_chaining import adapter, _invoke


def test_explicit_generation_adopts_existing_legacy_callback(adapter):
    fired = []
    adapter.register_post_delivery_callback('private-session', lambda: fired.append('legacy'))
    adapter.register_post_delivery_callback('private-session', lambda: fired.append('owned'), generation=9)
    callback = adapter.pop_post_delivery_callback('private-session', generation=9)
    _invoke(callback)
    assert fired == ['legacy', 'owned']
    assert 'private-session' not in adapter._post_delivery_callbacks
