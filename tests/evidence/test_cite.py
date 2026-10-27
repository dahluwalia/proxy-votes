"""M2.1: one snapshot per citation format."""

from pennant.core.models import Evidence, notice_ref
from pennant.evidence.cite import cite, notice_label


def test_notice_citation():
    ev = Evidence(source="notice", ref=notice_ref("Tesco_2026_Notice_of_AGM.pdf", 10),
                  quote="Chair: Barratt Redrow plc and ICE Clear Europe")
    assert cite(ev) == 'Tesco notice p.10: "Chair: Barratt Redrow plc and ICE Clear Europe"'


def test_notice_citation_collapses_whitespace_and_handles_missing_parts():
    ev = Evidence(source="notice", ref=notice_ref("Marks_and_Spencer_2026_Notice_of_AGM.pdf", 3),
                  quote="Tamara\n Ingram")
    assert cite(ev) == 'Marks and Spencer notice p.3: "Tamara Ingram"'
    assert cite(Evidence(source="notice", ref="ITV_2026_Notice_of_AGM.pdf")) == "ITV notice"


def test_companies_house_citation():
    url = "https://api.company-information.service.gov.uk/company/00445790/officers"
    assert cite(Evidence(source="ch", ref=url)) == f"Companies House: {url}"


def test_web_citation():
    ev = Evidence(source="web", ref="https://ir.capriholdings.com/", title="Capri Holdings IR",
                  quote="NYSE: CPRI")
    assert cite(ev) == 'Web: Capri Holdings IR https://ir.capriholdings.com/ — "NYSE: CPRI"'
    bare = Evidence(source="web", ref="https://example.com/x")
    assert cite(bare) == "Web: https://example.com/x"


def test_analyst_citation():
    ev = Evidence(source="analyst", ref="dec:3", quote="Moelis role is advisory")
    assert cite(ev) == 'Analyst (dec:3): "Moelis role is advisory"'


def test_long_quote_is_truncated():
    ev = Evidence(source="web", ref="https://x.test", quote="word " * 100)
    assert cite(ev).endswith('…"') and len(cite(ev)) < 300


def test_notice_labels():
    assert notice_label("Severn_Trent_2026_Notice_of_AGM.pdf") == "Severn Trent"
    assert notice_label("Acme_Holdings_2026_Notice_of_AGM.pdf") == "Acme Holdings"
