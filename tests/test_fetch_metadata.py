import re
import types

from abstract import ViURTestCase


def _req(headers, is_post=False):
    """Build a minimal stand-in for the Router/BrowseHandler the validator inspects."""
    return types.SimpleNamespace(
        request=types.SimpleNamespace(headers=headers),
        isPostRequest=is_post,
    )


class TestFetchMetaDataValidator(ViURTestCase):
    def setUp(self):
        super().setUp()
        from viur.core.config import conf
        conf.strict_mode = False
        # Snapshot globals the tests mutate, so we don't pollute other tests.
        self._orig_cors = conf.security.cors_origins
        self._orig_dev = conf.instance.is_dev_server

    def tearDown(self):
        from viur.core.config import conf
        conf.security.cors_origins = self._orig_cors
        conf.instance.is_dev_server = self._orig_dev
        super().tearDown()

    def _validate(self, headers, is_post=False):
        from viur.core.request import FetchMetaDataValidator
        return FetchMetaDataValidator.validate(_req(headers, is_post))

    # --- always-trusted sites ---
    def test_missing_header_allowed(self):
        self.assertIsNone(self._validate({}))

    def test_same_origin_allowed(self):
        self.assertIsNone(self._validate({"sec-fetch-site": "same-origin"}))

    def test_none_allowed(self):
        self.assertIsNone(self._validate({"sec-fetch-site": "none"}))

    def test_same_site_allowed_in_production(self):
        # same-site must be accepted regardless of dev/prod (previously dev-only).
        from viur.core.config import conf
        conf.instance.is_dev_server = False
        self.assertIsNone(self._validate({"sec-fetch-site": "same-site"}, is_post=True))

    # --- cross-site default policy ---
    def test_cross_site_non_navigation_rejected(self):
        result = self._validate(
            {"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}, is_post=True)
        self.assertEqual(result[0], 403)

    def test_cross_site_navigation_get_allowed(self):
        self.assertIsNone(self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        }))

    def test_cross_site_navigation_object_rejected(self):
        result = self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "object",
        })
        self.assertEqual(result[0], 403)

    # --- cross-site, but CORS-allow-listed ---
    def test_cross_site_cors_allowed_origin_allowed(self):
        from viur.core.config import conf
        conf.security.cors_origins = ["https://frontend.example.com"]
        self.assertIsNone(self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "Origin": "https://frontend.example.com",
        }, is_post=True))

    def test_cross_site_cors_origin_regex_allowed(self):
        from viur.core.config import conf
        conf.security.cors_origins = [re.compile(r"^https://[a-z]+\.example\.com$")]
        self.assertIsNone(self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "Origin": "https://app.example.com",
        }, is_post=True))

    def test_cross_site_non_allowlisted_origin_rejected(self):
        from viur.core.config import conf
        conf.security.cors_origins = ["https://frontend.example.com"]
        result = self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "Origin": "https://evil.example.org",
        }, is_post=True)
        self.assertEqual(result[0], 403)

    def test_cors_wildcard_allows_any_origin(self):
        from viur.core.config import conf
        conf.security.cors_origins = "*"
        self.assertIsNone(self._validate({
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "Origin": "https://anything.example.org",
        }, is_post=True))
