"""
Open Food Network publishes relations as literal IRI references on its DFC v1 endpoint.

Its connector exports the semantic id of each related resource ("#certification-4") as a
string, and the 1.16 context does not type dfc-b:isCertifiedBy as @id: the reference
reaches us as a plain literal instead of an @id. djangoldp-dfc reads a relation as the
IRI reference it means, resolved against the document base.
"""

from unittest import mock

import requests
from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Certification, Organization
from data_food_consortium.proxy.resource import ProxyRefreshParser, ResourceServerClient

SOURCE = "https://openfoodnetwork.example"
DOCUMENT_URL = f"{SOURCE}/api/dfc/enterprises"
ENTERPRISE_URL = f"{DOCUMENT_URL}/1"
V1_CONTEXT = "https://w3id.org/dfc/ontology/context/context_1.16.0.json"


def ofn_v1_document(certifications=None, certifies=None, enterprise=ENTERPRISE_URL):
    """
    A document shaped like the one DfcProvider::EnterprisesController#index serves: the
    enterprise alone, its certifications only named by the literal IRI reference the
    connector exports for them.
    """
    document = {
        "@context": V1_CONTEXT,
        "@graph": [
            {
                "@id": enterprise,
                "@type": "dfc-b:Enterprise",
                "dfc-b:name": "Farm",
            }
        ],
    }
    if certifications is not None:
        document["@graph"][0]["dfc-b:isCertifiedBy"] = certifications
    if certifies is not None:
        document["@graph"].append(
            {
                "@id": f"{DOCUMENT_URL}#certification-4",
                "@type": "dfc-b:Certification",
                "dfc-b:certifies": certifies,
            }
        )
    return document


def response(status, document=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.content = b"{}"
    resp.json.return_value = document or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def refresh(document):
    """Read the index as the hub does: fetched from its URL, with a token."""
    with mock.patch(
        "data_food_consortium.proxy.resource.requests.get"
    ) as get, mock.patch(
        "data_food_consortium.proxy.resource.KeycloakClient"
    ) as keycloak:
        keycloak.return_value.get_access_token.return_value = "token"
        # Discovery endpoint, then the index page.
        get.side_effect = [response(404), response(200, document)]
        client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
        client.scope_config = {"ReadEnterprise": "api/dfc/enterprises"}
        client.request_scope("ReadEnterprise")


class TestLiteralRelationIris(TestCase):
    def test_a_single_literal_certification_is_imported(self):
        """One literal "#certification-4" resolves to <document URL>#certification-4."""
        refresh(ofn_v1_document(certifications="#certification-4"))

        self.assertEqual(
            list(Organization.objects.values_list("proxy_of", flat=True)),
            [ENTERPRISE_URL],
        )
        certification = Certification.objects.get()
        self.assertEqual(certification.proxy_of, f"{DOCUMENT_URL}#certification-4")
        self.assertEqual(
            list(Organization.objects.get().certifications.all()), [certification]
        )

    def test_every_literal_of_a_to_many_relation_is_imported(self):
        """An array of literals yields one certification per literal, not per letter."""
        refresh(
            ofn_v1_document(certifications=["#certification-4", "#certification-5"])
        )

        self.assertEqual(
            sorted(Certification.objects.values_list("proxy_of", flat=True)),
            [
                f"{DOCUMENT_URL}#certification-4",
                f"{DOCUMENT_URL}#certification-5",
            ],
        )
        self.assertEqual(Organization.objects.get().certifications.count(), 2)

    def test_a_literal_foreign_key_is_imported(self):
        """A single-valued relation is resolved the same way (dfc-b:certifies)."""
        refresh(ofn_v1_document(certifies="#enterprise", enterprise="#enterprise"))

        organization = Organization.objects.get()
        self.assertEqual(organization.proxy_of, f"{DOCUMENT_URL}#enterprise")
        self.assertEqual(organization.name, "Farm")
        self.assertEqual(Certification.objects.get().certifies, organization)

    def test_a_literal_which_is_no_iri_reference_is_ignored(self):
        """A free-text literal designates no resource: the import still succeeds."""
        refresh(ofn_v1_document(certifications=["Organic, certified AB"]))

        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(Certification.objects.count(), 0)

    def test_a_literal_without_base_iri_is_ignored(self):
        """Without a base IRI the reference cannot be resolved: it is not imported."""
        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(
            ofn_v1_document(certifications="#certification-4")
        )

        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(Certification.objects.count(), 0)
