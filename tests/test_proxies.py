from __future__ import annotations

from rankrecovery.identity.proxies import ProxyPool, parse_proxy_line, parse_proxy_list, scrub_credentials


def test_parse_all_formats():
    text = """
    # comment
    http://1.1.1.1:8080
    https://proxy.example.com:443
    http://alice:s3cret@2.2.2.2:3128
    3.3.3.3:9000
    4.4.4.4:9001:bob:hunter2
    carol:pw%40rd@5.5.5.5:9002
    """
    result = parse_proxy_list(text)
    assert not result.errors, result.errors
    entries = result.entries
    assert [e.scheme for e in entries] == ["http", "https", "http", "http", "http", "http"]
    assert entries[0].url == "http://1.1.1.1:8080" and not entries[0].has_auth
    assert entries[2].has_auth and entries[2].url == "http://alice:s3cret@2.2.2.2:3128"
    assert entries[4].has_auth and entries[4].url.startswith("http://bob:hunter2@4.4.4.4:9001")
    assert entries[5].has_auth and entries[5].host == "5.5.5.5" and entries[5].port == 9002
    for e in entries:
        assert "s3cret" not in e.safe_id and "hunter2" not in e.safe_id and "pw" not in e.safe_id.split("#")[0]
        assert "@" not in e.safe_id
        assert "s3cret" not in repr(e) and "hunter2" not in str(e)


def test_bad_lines_reported_not_raised():
    result = parse_proxy_list("ftp://x:1\nnot a proxy\n1.1.1.1:99999\nhttp://user:pass@bad\n1.1.1.1:80\n1.1.1.1:80")
    assert len(result.entries) == 1
    reasons = [e.reason for e in result.errors]
    assert len(result.errors) == 5 and any("duplicate" in r for r in reasons)
    for err in result.errors:
        assert "pass" not in err.line or "***" in err.line


def test_parse_line_requires_port():
    try:
        parse_proxy_line("http://host")
    except ValueError as exc:
        assert "port" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_scrub_credentials():
    assert "s3cret" not in scrub_credentials("ProxyError: http://alice:s3cret@2.2.2.2:3128 refused")
    assert scrub_credentials("plain message") == "plain message"


def test_pool_rotation_and_disable():
    now = [1000.0]
    entries = parse_proxy_list("1.1.1.1:80\n2.2.2.2:80").entries
    pool = ProxyPool(entries, mode="rotate", failure_threshold=2, cooldown_seconds=60, clock=lambda: now[0])
    a = pool.next_proxy()
    b = pool.next_proxy()
    assert a is not None and b is not None and a.safe_id != b.safe_id
    pool.mark_failure(a)
    assert not pool.is_disabled(a)
    pool.mark_failure(a)
    assert pool.is_disabled(a)
    # only b is served while a cools down
    assert {pool.next_proxy().safe_id for _ in range(4)} == {b.safe_id}
    now[0] += 61
    assert not pool.is_disabled(a)
    pool.mark_success(a)
    status = pool.status()
    assert status[0]["consecutive_failures"] == 0 and status[0]["total_failures"] == 2
    assert all("1.1.1.1" in s["proxy"] or "2.2.2.2" in s["proxy"] for s in status)


def test_pool_sticky_and_direct():
    entries = parse_proxy_list("1.1.1.1:80\n2.2.2.2:80").entries
    sticky = ProxyPool(entries, mode="sticky")
    first = sticky.next_proxy()
    assert all(sticky.next_proxy().safe_id == first.safe_id for _ in range(3))
    assert sticky.rotate().safe_id != first.safe_id
    direct = ProxyPool(entries, mode="direct")
    assert not direct.enabled and direct.next_proxy() is None
    empty = ProxyPool([], mode="rotate")
    assert not empty.enabled and empty.next_proxy() is None
