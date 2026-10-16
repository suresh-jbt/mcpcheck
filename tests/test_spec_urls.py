"""Offline structure tests for ``spec_urls`` (Appendix B); the anchor-liveness test is ``network``-marked."""

from __future__ import annotations

import pytest

from mcpcheck import spec_urls

DEAD_HANDOFF_PATHS = (
    "/specification/2026-07-28/transports/streamable-http",
    "/specification/2026-07-28/errors",
    "/specification/2026-07-28/cache",
    "/specification/2026-07-28/security#origin-validation",
)
UNUSED_ANCHORS = (
    "schema#cacheableresult",
    "schema#error",
    "schema#toolannotations",
    "schema#listtoolsresult",
    "caching#choosing-a-cache-scope",
    "tools#structured-content",
)


def test_every_constant_starts_with_base() -> None:
    urls = spec_urls.all_urls()
    assert urls["SPEC_BASE"] == "https://modelcontextprotocol.io/specification/2026-07-28"
    assert len(urls) >= 70
    for name, url in urls.items():
        # the security best practices live under /docs/ (the /specification/ path is a 308)
        assert url.startswith((spec_urls.SPEC_BASE, spec_urls.DOCS_BASE)), name
        assert " " not in url, name
        assert url.count("#") <= 1, name
    assert spec_urls.SPEC_SECURITY_BEST_PRACTICES.startswith(spec_urls.DOCS_BASE)
    assert "/specification/" not in spec_urls.SPEC_SECURITY_BEST_PRACTICES


def test_dead_handoff_paths_absent() -> None:
    for url in spec_urls.all_urls().values():
        for dead in DEAD_HANDOFF_PATHS:
            assert not url.endswith(dead), url
        for unused in UNUSED_ANCHORS:
            assert not url.endswith(unused), url


def test_known_anchors() -> None:
    assert spec_urls.SPEC_META.endswith("/basic#_meta")
    assert spec_urls.SPEC_REF_RESOLUTION.endswith("/basic#$ref-resolution")
    assert spec_urls.SPEC_ORIGIN_FRAGMENT == spec_urls.SPEC_TRANSPORT + "#security-&-endpoint"
    assert "#" not in spec_urls.SPEC_TRANSPORT
    assert spec_urls.SPEC_STATELESSNESS.endswith("/basic#statelessness")
    assert spec_urls.SPEC_CACHEABLE_RESULTS.endswith("/server/utilities/caching#cacheable-results")
    assert spec_urls.SPEC_PROMPTS_CAPABILITIES.endswith("/server/prompts#capabilities")
    assert spec_urls.SPEC_DEPRECATED.endswith("/deprecated#deprecated")
    assert spec_urls.SPEC_LOGGING.endswith("/server/utilities/logging")


def test_constants_are_unique() -> None:
    urls = spec_urls.all_urls()
    assert len(set(urls.values())) == len(urls), "duplicate URL constants"


@pytest.mark.network
def test_anchors_resolve_online() -> None:  # pragma: no cover - opt-in: pytest -m network
    import httpx2

    # no redirect following: a moved page (3xx) must be re-pointed at its canonical URL
    with httpx2.Client(timeout=20, follow_redirects=False) as client:
        for name, url in spec_urls.all_urls().items():
            resp = client.get(url.split("#", 1)[0])
            assert resp.status_code == 200, (name, url, resp.headers.get("location"))
