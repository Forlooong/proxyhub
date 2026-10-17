"""Stable subscription references reuse local node identities and legacy defaults."""
import base64
from copy import deepcopy

import pytest

from proxyhub import nodes, subscriptions
from proxyhub.proxy import validate_spec


@pytest.fixture
def config():
    server = validate_spec({
        'site_domain': 'site.example.test', 'subscription_domain': 'sub.example.test',
        'public_address': '203.0.113.10', 'uuid': 'a875d3ac-980f-4a0c-929e-78cc678429f1',
        'private_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
        'public_key': base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip('='),
        'short_id': '0123456789abcdef',
        'certificate_path': '/var/lib/proxyhub/certificates/0123456789abcdef/fullchain.pem',
        'key_path': '/var/lib/proxyhub/certificates/0123456789abcdef/privkey.pem',
    })
    value = {'server': server, 'subscription': {
        'id': 'default', 'token': 'T' * 43, 'name': 'Legacy subscription',
        'userinfo': {}, 'update_interval_hours': 6}, 'overrides': {'mixed-port': 17891}}
    return nodes.change(value, 'nodes.create', {'name': 'Shared SS', 'protocol': 'ss',
                        'port': 31444, 'options': {'method': 'aes-128-gcm'}})


def test_legacy_projection_does_not_rewrite_default_or_reveal_credentials(config):
    before = deepcopy(config)
    profile = subscriptions.get_profile(config)
    assert config == before and 'subscriptions' not in config
    assert profile['node_ids'] is None and profile['host'] == config['server']['subscription_domain']
    assert profile['path_prefix'] == '/sub' and profile['formats'] == ['yaml', 'raw']
    assert profile['overrides'] == config['overrides'] and profile['token'] == config['subscription']['token']
    public = subscriptions.public_resources(config)
    assert 'token' not in public['profiles'][0]
    assert all(set(node) == {'id', 'name', 'protocol', 'enabled'} for node in public['nodes'])


def test_create_copy_and_partial_update_share_node_without_changing_resources(config):
    node_id = config['nodes'][0]['id']
    created = subscriptions.change(config, 'subscription.create', {
        'name': 'Second', 'node_ids': [node_id], 'formats': ['yaml'],
        'overrides': {'mode': 'global'}, 'userinfo': {'expire': 2_000_000_000}})
    extra = created['subscriptions'][0]
    assert config.get('subscriptions') is None
    assert created['nodes'] == config['nodes'] and created['server'] == config['server']
    assert created['subscription'] == config['subscription'] and created['overrides'] == config['overrides']
    copied = subscriptions.change(created, 'subscription.copy', {'subscription_id': 'default', 'name': 'Copy'})
    copy = copied['subscriptions'][1]
    assert copy['node_ids'] == subscriptions.available_node_ids(config)
    assert copy['id'] not in {'default', extra['id']} and copy['token'] != config['subscription']['token']
    assert copy['aliases'] == []
    changed = subscriptions.change(copied, 'subscription.update', {'subscription_id': extra['id'], 'name': 'Renamed'})
    assert changed['subscriptions'][0]['name'] == 'Renamed'
    assert changed['subscriptions'][0]['node_ids'] == [node_id]
    assert changed['nodes'] == config['nodes']


def test_alias_retirement_and_token_rotation_leave_node_credentials_untouched(config):
    updated = subscriptions.change(config, 'subscription.update', {'path_prefix': '/personal/sub'})
    profile = subscriptions.get_profile(updated)
    assert profile['aliases'][0]['path_prefix'] == '/sub'
    added = subscriptions.change(updated, 'subscription.alias.add', {
        'subscription_id': 'default', 'host': config['server']['site_domain'], 'path_prefix': '/links'})
    rotated = subscriptions.change(added, 'subscription.token.rotate', {'subscription_id': 'default'})
    current = subscriptions.get_profile(rotated)
    assert current['token'] != config['subscription']['token'] and current['aliases'] == subscriptions.get_profile(added)['aliases']
    retired = subscriptions.change(rotated, 'subscription.alias.retire', {
        'subscription_id': 'default', 'alias_id': current['aliases'][0]['id']})
    assert len(subscriptions.get_profile(retired)['aliases']) == 1
    assert retired['nodes'] == config['nodes'] and retired['server'] == config['server']
    promoted = subscriptions.change(updated, 'subscription.update', {'path_prefix': '/sub'})
    assert subscriptions.get_profile(promoted)['aliases'][0]['path_prefix'] == '/personal/sub'


def test_disabled_references_are_retained_but_active_profile_needs_available_node(config):
    node_id = config['nodes'][0]['id']
    created = subscriptions.change(config, 'subscription.create', {'name': 'Second', 'node_ids': [node_id]})
    disabled_node = nodes.change(created, 'nodes.disable', {'node_id': node_id})
    with pytest.raises(ValueError, match='no enabled selected nodes'):
        subscriptions.normalize(disabled_node)
    disabled_profile = subscriptions.change(created, 'subscription.disable', {'subscription_id': created['subscriptions'][0]['id']})
    disabled_both = nodes.change(disabled_profile, 'nodes.disable', {'node_id': node_id})
    assert subscriptions.normalize(disabled_both)[1]['node_ids'] == [node_id]
    with pytest.raises(ValueError, match='no enabled selected nodes'):
        subscriptions.change(disabled_both, 'subscription.enable', {'subscription_id': created['subscriptions'][0]['id']})


@pytest.mark.parametrize('field,value', [
    ('node_ids', []), ('node_ids', ['unknown']), ('node_ids', ['local-reality', 'local-reality']),
    ('formats', []), ('formats', ['yaml', 'yaml']), ('formats', ['json']),
    ('path_prefix', '/sub/'), ('path_prefix', '/sub/%2f'), ('path_prefix', '/__proxyhub-status'),
    ('path_prefix', '/status'),
    ('path_prefix', '/'+'a'*65),
    ('path_prefix', '/.well-known/acme-challenge'), ('host', 'unmanaged.example.test'),
    ('userinfo', {'upload': 1}), ('userinfo', {'expire': True}), ('overrides', {'proxies': []}),
])
def test_invalid_profile_fields_fail_closed(config, field, value):
    values = {'name': 'Second', 'node_ids': ['local-reality'], field: value}
    with pytest.raises(ValueError):
        subscriptions.change(config, 'subscription.create', values)


def test_duplicate_identity_token_and_missing_node_reference_are_rejected(config):
    created = subscriptions.change(config, 'subscription.create', {'name': 'Second', 'node_ids': ['local-reality']})
    created['subscriptions'][0]['token'] = config['subscription']['token']
    with pytest.raises(ValueError, match='Duplicate'):
        subscriptions.normalize(created)
    created['subscriptions'][0]['token'] = 'N' * 43
    created['subscriptions'][0]['id'] = 'default'
    with pytest.raises(ValueError, match='default identity'):
        subscriptions.normalize(created)


def test_old_six_field_update_keeps_token_and_original_overrides_location(config):
    updated = subscriptions.change(config, 'subscription.update', {
        'name': 'Metadata', 'userinfo': {'upload': 0, 'download': 1, 'total': 1024, 'expire': 2_000_000_000},
        'update_interval_hours': 12, 'reset_day': 31, 'reset_at': None, 'overrides': {'ipv6': True}})
    assert updated['subscription']['token'] == config['subscription']['token']
    assert 'overrides' not in updated['subscription'] and updated['overrides'] == {'ipv6': True}
    assert updated['nodes'] == config['nodes'] and updated['server'] == config['server']


@pytest.mark.parametrize('enabled', [True, False])
def test_alias_retirement_cannot_close_another_subscription_route(config, enabled):
    shared = subscriptions.change(config, 'subscription.create', {'name': 'Second', 'node_ids': ['local-reality']})
    if not enabled:
        shared = subscriptions.change(shared, 'subscription.disable', {'subscription_id': shared['subscriptions'][0]['id']})
    moved = subscriptions.change(shared, 'subscription.update', {'path_prefix': '/new-sub'})
    alias_id = subscriptions.get_profile(moved)['aliases'][0]['id']
    before = deepcopy(moved)
    with pytest.raises(ValueError, match='referenced by another subscription'):
        subscriptions.change(moved, 'subscription.alias.retire', {'subscription_id': 'default', 'alias_id': alias_id})
    assert moved == before
