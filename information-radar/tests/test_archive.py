"""FreshRSS + Zotero 归档契约测试。

业务契约：
- FreshRSS：普通内容归档（subscription/edit）
- Zotero：高价值论文收藏（Web API）
- 单条失败不影响其他条目
"""
from __future__ import annotations

import httpx
import respx

from paper_bridge.archive.freshrss import FreshRSSArchiver
from paper_bridge.archive.zotero import ZOTERO_API, ZoteroArchiver
from paper_bridge.report.brief import BriefItem


def make_brief_item(**kw) -> BriefItem:
    base = dict(
        title="Test Paper",
        authors=["Alice Smith"],
        url="http://example.com/paper",
        doi="10.1/test",
        venue="ICSE",
        date="2026-07-05",
        status="正式论文",
        source_type="crossref",
        total_score=85,
        tier="selected",
        has_full_text=True,
        summary={"research_question": "Test RQ", "recommendation": "值得复现"},
    )
    base.update(kw)
    return BriefItem(**base)


class TestFreshRSSArchiver:
    @respx.mock
    def test_login_success(self):
        respx.post("http://freshrss:80/api/greader.php/accounts/ClientLogin").mock(
            return_value=httpx.Response(200, text="Auth=token123\nSID=abc")
        )
        archiver = FreshRSSArchiver(base_url="http://freshrss:80", api_user="admin", api_password="pw")
        assert archiver._login() is True
        assert archiver._auth_token == "token123"
        archiver.close()

    @respx.mock
    def test_login_failure_returns_false(self):
        respx.post("http://freshrss:80/api/greader.php/accounts/ClientLogin").mock(
            return_value=httpx.Response(401)
        )
        archiver = FreshRSSArchiver(base_url="http://freshrss:80", api_user="bad", api_password="bad")
        assert archiver._login() is False
        archiver.close()

    @respx.mock
    def test_archive_item_success(self):
        respx.post("http://freshrss:80/api/greader.php/accounts/ClientLogin").mock(
            return_value=httpx.Response(200, text="Auth=tok")
        )
        respx.post("http://freshrss:80/api/greader.php/reader/api/0/subscription/edit").mock(
            return_value=httpx.Response(200, text="OK")
        )
        archiver = FreshRSSArchiver(base_url="http://freshrss:80", api_user="admin", api_password="pw")
        assert archiver.archive_item(make_brief_item()) is True
        archiver.close()

    @respx.mock
    def test_archive_batch_counts_success_failure(self):
        respx.post("http://freshrss:80/api/greader.php/accounts/ClientLogin").mock(
            return_value=httpx.Response(200, text="Auth=tok")
        )
        # 第一条 200，第二条 500
        def handler(req):
            if "paper1" in str(req.content):
                return httpx.Response(200, text="OK")
            return httpx.Response(500)

        respx.post("http://freshrss:80/api/greader.php/reader/api/0/subscription/edit").mock(side_effect=handler)
        archiver = FreshRSSArchiver(base_url="http://freshrss:80", api_user="admin", api_password="pw")
        items = [
            make_brief_item(title="paper1", url="http://example.com/1"),
            make_brief_item(title="paper2", url="http://example.com/2"),
        ]
        ok, fail = archiver.archive_batch(items)
        assert ok == 1
        assert fail == 1
        archiver.close()


class TestZoteroArchiver:
    @respx.mock
    def test_archive_item_success(self):
        respx.post(f"{ZOTERO_API}/users/12345/items").mock(
            return_value=httpx.Response(200, json={"success": {"0": "ABC123"}})
        )
        archiver = ZoteroArchiver(
            user_id="12345", api_key="key", collection_id="col1", proxy=None
        )
        assert archiver.archive_item(make_brief_item()) is True
        archiver.close()

    @respx.mock
    def test_archive_item_failure_returns_false(self):
        respx.post(f"{ZOTERO_API}/users/12345/items").mock(
            return_value=httpx.Response(403)
        )
        archiver = ZoteroArchiver(
            user_id="12345", api_key="bad", proxy=None
        )
        assert archiver.archive_item(make_brief_item()) is False
        archiver.close()

    @respx.mock
    def test_archive_batch_isolates_failures(self):
        def handler(req):
            if "paper1" in str(req.content):
                return httpx.Response(200, json={"success": {}})
            return httpx.Response(500)

        respx.post(f"{ZOTERO_API}/users/12345/items").mock(side_effect=handler)
        archiver = ZoteroArchiver(user_id="12345", api_key="key", proxy=None)
        items = [
            make_brief_item(title="paper1", url="http://1"),
            make_brief_item(title="paper2", url="http://2"),
        ]
        ok, fail = archiver.archive_batch(items)
        assert ok == 1
        assert fail == 1
        archiver.close()

    @respx.mock
    def test_api_key_in_header(self):
        route = respx.post(f"{ZOTERO_API}/users/12345/items").mock(
            return_value=httpx.Response(200, json={})
        )
        archiver = ZoteroArchiver(user_id="12345", api_key="secret-key", proxy=None)
        archiver.archive_item(make_brief_item())
        assert route.calls.last.request.headers["Zotero-API-Key"] == "secret-key"
        archiver.close()

    @respx.mock
    def test_doi_in_payload(self):
        route = respx.post(f"{ZOTERO_API}/users/12345/items").mock(
            return_value=httpx.Response(200, json={})
        )
        archiver = ZoteroArchiver(user_id="12345", api_key="key", proxy=None)
        archiver.archive_item(make_brief_item(doi="10.1145/test.001"))
        import json

        payload = json.loads(route.calls.last.request.content)
        assert payload[0]["data"]["DOI"] == "10.1145/test.001"
        archiver.close()
