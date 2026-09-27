"""v9.22: contact keys of an organisation — e-mail addresses and web domains (decision #64)."""

from hh_scout.pipeline.contacts import SHARED_DOMAINS, contact_keys, domain_of, normalize_email


def test_email_is_normalised_and_junk_rejected():
    assert normalize_email(" Sales@Firm.RU. ") == "sales@firm.ru"
    assert normalize_email("info@завод.рф") == "info@завод.рф"
    assert normalize_email("not an address") is None and normalize_email(None) is None and normalize_email("") is None


def test_domain_of_urls_and_addresses_strips_www_only():
    assert domain_of("http://www.amperika.com") == "amperika.com"
    assert domain_of("https://interrao-oco.ru/") == "interrao-oco.ru"
    assert domain_of("firm.ru/contacts?x=1") == "firm.ru"
    assert domain_of("sales@Firm.ru") == "firm.ru"
    assert domain_of("sales@mail.firm.ru") == "mail.firm.ru"          # no registrable-domain collapsing
    assert domain_of("") is None and domain_of(None) is None and domain_of("localhost") is None


def test_shared_hosts_identify_nobody():
    assert "mail.ru" in SHARED_DOMAINS and "hh.ru" in SHARED_DOMAINS
    assert domain_of("info@mail.ru") is None and domain_of("https://vk.com/firm") is None
    assert domain_of("https://hh.ru/employer/1") is None


def test_contact_keys_combine_addresses_and_sites():
    keys = contact_keys(["Info@Firm.RU", "boss@gmail.com", "junk"], ["http://www.firm.ru/", None, "https://t.me/firm"])
    assert keys == {("email", "info@firm.ru"), ("domain", "firm.ru"), ("email", "boss@gmail.com")}
    assert contact_keys() == set()
