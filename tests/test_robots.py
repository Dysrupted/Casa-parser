from radar.robots import Robots

MUBAWAB = """
User-agent: MJ12bot
Disallow: /

User-agent: *
Disallow: /login
Disallow: /*:
Disallow: /*?n=1
"""
SAROUTY = """User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
Crawl-delay: 10
Disallow: /*?s=
"""
UA = "CasaRadar/0.1 (personal low-volume research bot)"


def test_wildcards_that_urllib_misses():
    r = Robots(MUBAWAB)
    assert r.can_fetch(UA, "https://www.mubawab.ma/fr/sd/casablanca/anfa/appartements-a-vendre")
    assert not r.can_fetch(UA, "https://www.mubawab.ma/fr/st/casablanca/appartements-a-vendre:p:2")
    assert not r.can_fetch(UA, "https://www.mubawab.ma/fr/x?n=1")
    assert r.can_fetch(UA, "https://www.mubawab.ma/fr/a/8421267/appartement")
    assert not r.can_fetch("MJ12bot/1.0", "https://www.mubawab.ma/fr/a/1/x")


def test_allow_longest_match_and_crawl_delay():
    r = Robots(SAROUTY)
    assert r.can_fetch(UA, "https://x/wp-admin/admin-ajax.php") and not r.can_fetch(UA, "https://x/wp-admin/x")
    assert not r.can_fetch(UA, "https://x/fr/?s=anfa")
    assert r.crawl_delay(UA) == 10


def test_end_anchor_and_empty():
    r = Robots("User-agent: *\nDisallow: /*.pdf$\nDisallow:\n")
    assert not r.can_fetch(UA, "https://x/a.pdf") and r.can_fetch(UA, "https://x/a.pdf?x=1")
    assert Robots("").can_fetch(UA, "https://x/anything")
    assert not Robots(disallow_all=True).can_fetch(UA, "https://x/")
