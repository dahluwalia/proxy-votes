"""M4.3 on the recorded fixtures: 50 notice mentions become 47 people (H1, H4, H5).

Runs the real extract stage (LlamaExtract fixtures), the CH stage (tests/fixtures/ch/)
and lane C's anchor/expand, all offline. Skips until the CH fixtures are recorded.
"""

import pytest

from pennant.core import db
from pennant.core.config import FIXTURES_DIR
from pennant.extract import validate

pytest.importorskip("pennant.dedup.anchor", reason="lane C's M4.2 not landed yet")
from pennant.ch import client  # noqa: E402
from pennant.dedup import cluster  # noqa: E402

pytestmark = pytest.mark.skipif(
    not any((FIXTURES_DIR / "ch").glob("**/*.json")), reason="CH fixtures not recorded yet"
)


@pytest.fixture
def clustered(ctx):
    if client.load_fixtures(ctx.conn) == 0:
        pytest.skip("CH fixtures not recorded yet")
    assert validate.run(ctx).ok
    assert client.run(ctx).ok
    report = cluster.run(ctx)
    people = db.get_outputs(ctx.conn, ctx.run_id, "person")
    officers = {o.officer_id: o for o in db.get_outputs(ctx.conn, ctx.run_id, "officer")}
    names = {e.mention_id: e.name for e in db.get_outputs(ctx.conn, ctx.run_id, "roster")}
    return report, people, officers, names


def _person(people, names, name):
    hits = [p for p in people if any(name in names[m] for m in p.mention_ids)]
    assert len(hits) == 1, f"{name}: {[p.person_id for p in hits]}"
    return hits[0]


def test_fifty_mentions_become_47_people(clustered):
    report, people, _, _ = clustered
    assert report.counts["mentions"] == 50
    assert len(people) == 47


@pytest.mark.parametrize("name", ["Chris Kennedy", "Sharmila Nebhrajani", "Tamara Ingram"])
def test_h4_same_person_in_two_notices(clustered, name):
    _, people, _, names = clustered
    assert len(_person(people, names, name).mention_ids) == 2


def test_h5_only_the_kennedy_with_the_anchor_dob_is_linked(clustered):
    _, people, officers, names = clustered
    kennedy = _person(people, names, "Chris Kennedy")
    dobs = {(officers[o].dob_month, officers[o].dob_year) for o in kennedy.officer_ids}
    assert len(dobs) == 1


def test_h1_ken_and_gerry_murphy_stay_apart(clustered):
    _, people, _, names = clustered
    ken, gerry = _person(people, names, "Ken Murphy"), _person(people, names, "Gerry Murphy")
    assert ken.person_id != gerry.person_id
    assert not set(ken.officer_ids) & set(gerry.officer_ids)


def test_d2_people_cite_the_links_that_joined_them(clustered):
    """Chris Kennedy is one person because both notices anchor to the same CH officer:
    his evidence cites both issuers' officer lists, once each."""
    _, people, _, names = clustered
    kennedy = _person(people, names, "Chris Kennedy")
    refs = [e.ref for e in kennedy.evidence]
    assert len(refs) == len(set(refs)) == 2
    assert any("/company/00445790/officers" in r for r in refs)
    assert any("/company/04967001/officers" in r for r in refs)
    assert all(p.evidence for p in people if p.officer_ids)
