"""Every outgoing HTTP request carries the DFC_HTTP_TIMEOUT deadline.

A data-server or a Keycloak endpoint which accepts the connection and never answers used to
block the caller for ever: the cache refresh of the hub then holds its per-source lock, and
every later refresh of that source waits behind it. Each request therefore passes
`timeout=settings.DFC_HTTP_TIMEOUT` (30 seconds by default).
"""

from unittest import mock

import requests
from django.test import TestCase, override_settings

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.proxy.keycloak import (
    KeycloakClient,
    KeycloakResourceServerAuthentication,
)
from data_food_consortium.proxy.resource import ResourceServerClient

SOURCE = "https://data-server.example"


def response(status, document=None):
    result = mock.Mock()
    result.status_code = status
    result.content = b"{}"
    result.text = "{}"
    result.json.return_value = document or {}
    if status >= 400:
        result.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=result
        )
    else:
        result.raise_for_status.return_value = None
    return result


def timeouts(appels):
    return [appel.kwargs.get("timeout") for appel in appels]


class TestDataServerTimeouts(TestCase):
    def test_discovery_and_scope_read_carry_the_deadline(self):
        with mock.patch(
            "data_food_consortium.proxy.resource.requests.get"
        ) as get, mock.patch(
            "data_food_consortium.proxy.resource.KeycloakClient"
        ) as keycloak, mock.patch(
            "data_food_consortium.proxy.resource.ProxyRefreshParser"
        ):
            keycloak.return_value.get_access_token.return_value = "token"
            get.side_effect = [response(404), response(200, {"@graph": []})]
            client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
            client.scope_config = {"ReadEnterprise": "organizations/"}
            client.request_scope("ReadEnterprise")

        # Discovery, then the endpoint.
        self.assertEqual(timeouts(get.call_args_list), [30, 30])

    @override_settings(DFC_HTTP_TIMEOUT=5)
    def test_the_deadline_is_configurable(self):
        with mock.patch(
            "data_food_consortium.proxy.resource.requests.get"
        ) as get:
            get.return_value = response(404)
            ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)

        self.assertEqual(timeouts(get.call_args_list), [5])

    def test_a_timed_out_request_still_fails_as_a_network_error(self):
        """The deadline raises `RequestException`, which the caller already journals."""
        with mock.patch(
            "data_food_consortium.proxy.resource.requests.get"
        ) as get, mock.patch(
            "data_food_consortium.proxy.resource.KeycloakClient"
        ) as keycloak:
            keycloak.return_value.get_access_token.return_value = "token"
            # Discovery answers, the scope read times out.
            get.side_effect = [
                response(404),
                requests.exceptions.ReadTimeout("30 seconds"),
            ]
            client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
            client.scope_config = {"ReadEnterprise": "organizations/"}
            with mock.patch.object(client, "_log_dataserver_import_error") as journal:
                client.request_all_scopes()

        self.assertEqual(journal.call_count, 1)
        self.assertEqual(journal.call_args.args[2].value, "request_err")


@override_settings(
    KEYCLOAK_URL="https://keycloak.example/realms/test/protocol/openid-connect/token",
    KEYCLOAK_CLIENT_ID="client",
    KEYCLOAK_CLIENT_SECRET="secret",
)
class TestKeycloakTimeout(TestCase):
    def test_the_token_request_carries_the_deadline(self):
        with mock.patch(
            "data_food_consortium.proxy.keycloak.requests.post"
        ) as post:
            post.return_value = response(200, {"access_token": "token"})
            self.assertEqual(KeycloakClient("ReadEnterprise").get_access_token(), "token")

        self.assertEqual(timeouts(post.call_args_list), [30])

    def test_the_jwks_request_carries_the_deadline(self):
        with mock.patch(
            "data_food_consortium.proxy.keycloak.requests.get"
        ) as get, mock.patch(
            "data_food_consortium.proxy.keycloak.authlib_jwt.decode"
        ):
            get.return_value = response(200, {"keys": []})
            KeycloakResourceServerAuthentication().get_valid_claims("encoded")

        self.assertEqual(timeouts(get.call_args_list), [30])
