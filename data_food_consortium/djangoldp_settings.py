"""This module is loaded by DjangoLDP core during setup."""

import os

MIDDLEWARE = []
INSTALLED_APPS = []

# NOTE: Package variables are prefixed DFC_XYZ

# Keycloak configuration
KEYCLOAK_URL = f"{os.getenv('KEYCLOAK_URL')}/realms/{os.getenv('KEYCLOAK_REALM')}/protocol/openid-connect/token"
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET")

# Set to False to disable storing logs of dataserver imports in the database.
DFC_STORE_IMPORT_REPORTS = True

# Defines the default read scopes, if not overridden during the Discovery mechanism.
# TODO: ReadOrders and ReadProducts aren't implemented on the staging data-server, but should be included.
# TODO: Scopes could be configured per-dataserver by using the DataServer model.
DFC_KEYCLOAK_READ_SCOPES = {"ReadEnterprise": "organizations/"}
DFC_REQUESTED_SCOPES_DOCUMENT = (
    "https://cdn.startinblox.com/owl/dfc/taxonomies/cqcm.jsonld"
)
# If set to True, anonymous access will be disabled, and users will only be able to see data which they have been
# granted access to by a data owner.
DFC_USER_GRANTS_ENABLED = False

# How a cache refresh decides which previously proxied objects to discard.
# "domain" (default, historical behaviour): after each import, discard every object of the
#   imported models, from the same data-server domain, that this import did not see.
# "endpoint": discard only the objects that the previous import of the same endpoint saw
#   and this one did not. Needed when one data-server exposes several endpoints for the
#   same models (one per organization, for instance), and when a later import no longer
#   contains a model at all (a scope was revoked).
DFC_PROXY_CLEANUP = "domain"
# With DFC_PROXY_CLEANUP = "endpoint": when an endpoint answers 403, discard what the
# previous import of that endpoint brought, as the data owner has withdrawn access.
DFC_PROXY_FORGET_ON_FORBIDDEN = False
# Deadline of every outgoing HTTP request, in seconds: Keycloak token and JWKS endpoints,
# data-server discovery and scope reads. Without it a data-server which accepts the
# connection and never answers blocks the caller for ever; with it the request fails, and
# the caller logs the network error (ResourceImportFailure.NETWORK_ERROR) as usual.
DFC_HTTP_TIMEOUT = 30

LDP_RDF_CONTEXT = (
    "https://cdn.jsdelivr.net/gh/datafoodconsortium/ontology/context/context_2.0.0.json"
)
LDP_RDF_CONTEXT_V1 = "https://cdn.jsdelivr.net/gh/datafoodconsortium/ontology/context/context_1.16.0.json"

MIDDLEWARE = ["data_food_consortium.middleware.DisbaleJWTMiddleware"]

DATA_UPLOAD_MAX_MEMORY_SIZE = 5242880
