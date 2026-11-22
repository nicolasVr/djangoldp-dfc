from unittest import mock

import requests
from django.conf import settings
from django.test import TestCase, override_settings

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Organization, SuppliedProduct
from data_food_consortium.proxy.resource import ProxyRefreshParser, ResourceServerClient

SOURCE = "https://data-server.example"


def organization(number, products):
    """One organization of a data-server with its products, as one document."""
    base = f"{SOURCE}/organizations/{number}"
    graph = [{"@id": base, "@type": "dfc-b:Organization", "dfc-b:name": f"Farm {number}"}]
    graph += [
        {"@id": f"{base}/supplied_products/{p}", "@type": "dfc-b:SuppliedProduct", "dfc-b:name": f"Product {p}"}
        for p in products
    ]
    return {"@context": settings.LDP_RDF_CONTEXT, "@graph": graph}


def response(status, document=None, empty=False):
    resp = mock.Mock()
    resp.status_code = status
    resp.content = b"" if empty else b"{}"
    resp.json.return_value = document or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def refresh(path, http_response):
    """Run the real cache refresh of one endpoint against a mocked data-server."""
    with mock.patch("data_food_consortium.proxy.resource.requests.get") as get, mock.patch(
        "data_food_consortium.proxy.resource.KeycloakClient"
    ) as keycloak:
        keycloak.return_value.get_access_token.return_value = "token"
        get.side_effect = [response(404), http_response]  # discovery, then the endpoint
        client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
        client.scope_config = {"ReadEnterprise": path}
        try:
            client.request_scope("ReadEnterprise")
        except requests.exceptions.HTTPError:
            pass


def names(model):
    return sorted(model.objects.values_list("proxy_of", flat=True))


class TestDomainCleanup(TestCase):
    """The historical behaviour, kept as the default."""

    def test_second_endpoint_of_same_domain_replaces_the_first(self):
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/2", response(200, organization(2, [3])))

        self.assertEqual(names(Organization), [f"{SOURCE}/organizations/2"])

    def test_update_webhook_does_not_discard_other_resources(self):
        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(organization(1, [1, 2, 3]))
        changed = organization(1, [2])
        changed["@graph"] = changed["@graph"][1:]  # the changed product only
        ProxyRefreshParser(SOURCE, ResourceImportSource.UPDATE_WEBHOOK).parse(changed)

        self.assertEqual(SuppliedProduct.objects.count(), 3)


@override_settings(DFC_PROXY_CLEANUP="endpoint")
class TestEndpointCleanup(TestCase):
    def test_endpoints_of_same_domain_coexist(self):
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/2", response(200, organization(2, [3])))

        self.assertEqual(Organization.objects.count(), 2)
        self.assertEqual(SuppliedProduct.objects.count(), 3)

    def test_resource_no_longer_served_is_discarded(self):
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/1", response(200, organization(1, [1])))

        self.assertEqual(names(SuppliedProduct), [f"{SOURCE}/organizations/1/supplied_products/1"])

    def test_model_absent_from_new_import_is_discarded(self):
        """ReadProducts revoked: the endpoint now returns the organization alone."""
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/1", response(200, organization(1, [])))

        self.assertEqual(SuppliedProduct.objects.count(), 0)
        self.assertEqual(Organization.objects.count(), 1)

    def test_empty_body_is_an_empty_import(self):
        """OFN answers 200 with no body once the last enterprise has withdrawn access."""
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/1", response(200, empty=True))

        self.assertEqual(Organization.objects.count(), 0)
        self.assertEqual(SuppliedProduct.objects.count(), 0)

    def test_forbidden_keeps_data_by_default(self):
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/1", response(403))

        self.assertEqual(SuppliedProduct.objects.count(), 2)

    @override_settings(DFC_PROXY_FORGET_ON_FORBIDDEN=True)
    def test_forbidden_discards_when_configured(self):
        refresh("organizations/1", response(200, organization(1, [1, 2])))
        refresh("organizations/2", response(200, organization(2, [3])))
        refresh("organizations/1", response(403))

        self.assertEqual(names(Organization), [f"{SOURCE}/organizations/2"])
        self.assertEqual(SuppliedProduct.objects.count(), 1)
